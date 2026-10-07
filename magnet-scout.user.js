// ==UserScript==
// @name         Magnet Scout
// @namespace    https://github.com/alongyou/magnet-scout
// @version      4.2.0
// @description  Inspect magnet links, view tracker progress, and summarize resource availability.
// @author       alongyou
// @license      MIT
// @homepageURL  https://github.com/alongyou/magnet-scout
// @supportURL   https://github.com/alongyou/magnet-scout/issues
// @updateURL    https://raw.githubusercontent.com/alongyou/magnet-scout/main/magnet-scout.meta.js
// @downloadURL  https://raw.githubusercontent.com/alongyou/magnet-scout/main/magnet-scout.user.js
// @match        *://*/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      magnet-scount.local
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(() => {
    'use strict';
    if (window.__magnetQualityProbeV4) return;
    window.__magnetQualityProbeV4 = true;

    const API_URL = 'http://magnet-scount.local:8765/api/magnets/jobs';
    const FALLBACK_API_URL = 'http://127.0.0.1:8765/api/magnets/jobs';
    const BATCH_SIZE = 8;
    const ENABLE_DHT = true;
    const GOOD_SEEDS = 20;
    const records = new Map();
    const links = new Map();
    let scanTimer, postTimer, busy = false, expanded = false;
    let panel, summaryButton, details;
    const colors = { queued: '#64748b', checking: '#2563eb', good: '#15803d',
        seeded: '#15803d', dhtpeers: '#0369a1', peers: '#a16207', empty: '#64748b', unknown: '#64748b', error: '#b91c1c' };

    function cleanText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    }

    function canonicalHash(value) {
        value = value.toUpperCase();
        if (/^[0-9A-F]{40}$/.test(value)) return value;
        if (!/^[A-Z2-7]{32}$/.test(value)) return null;
        let bits = 0, buffer = 0, output = '';
        for (const char of value) {
            buffer = (buffer << 5) | 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'.indexOf(char);
            bits += 5;
            if (bits >= 8) {
                bits -= 8;
                output += ((buffer >> bits) & 255).toString(16).padStart(2, '0');
                buffer &= (1 << bits) - 1;
            }
        }
        return output.toUpperCase();
    }

    function parse(anchor) {
        try {
            const magnet = anchor.getAttribute('href').trim();
            const url = new URL(magnet);
            const xt = url.searchParams.getAll('xt').find(x => /^urn:btih:/i.test(x));
            const hash = xt && canonicalHash(xt.slice(9));
            if (!hash) return null;
            return { info_hash: hash, magnet,
                title: cleanText(url.searchParams.get('dn') || anchor.title || anchor.textContent || document.title),
                trackers: [...new Set(url.searchParams.getAll('tr').filter(Boolean))], source_url: location.href };
        } catch { return null; }
    }

    function visibleRecords() {
        return [...records.values()].filter(record => record.elements.size);
    }

    function ensurePanel() {
        if (panel?.isConnected) return;
        panel = document.createElement('div');
        panel.dataset.magnetProbeUi = 'panel';
        panel.style.cssText = 'all:initial!important;position:fixed!important;right:16px!important;bottom:16px!important;z-index:2147483647!important;';
        const root = panel.attachShadow({ mode: 'open' });
        root.innerHTML = `<style>
            :host{font:13px/1.5 system-ui,sans-serif;color:#1e293b}
            *{box-sizing:border-box}button{font:inherit;cursor:pointer}
            .box{font:13px/1.5 system-ui,sans-serif;color:#1e293b;width:max-content;max-width:calc(100vw - 32px);background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 4px 24px #0002;overflow:hidden}
            #summary{display:block;width:100%;border:0;background:#0f172a;color:#fff;padding:10px 14px;text-align:left;white-space:nowrap}
            #details{width:380px;max-width:calc(100vw - 32px);padding:12px;max-height:65vh;overflow:auto}
            [hidden]{display:none!important}.stats{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.stats span{background:#f1f5f9;border-radius:5px;padding:3px 7px}
            .row{border-top:1px solid #e2e8f0;padding:9px 0}.title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155}.status{font-size:12px}
            .note{font-size:11px;color:#64748b}#retry{border:1px solid #cbd5e1;background:#fff;border-radius:5px;padding:4px 8px;float:right}
        </style><div class="box"><button id="summary" aria-expanded="false"></button>
        <div id="details" hidden></div></div>`;
        summaryButton = root.getElementById('summary');
        details = root.getElementById('details');
        summaryButton.addEventListener('click', () => {
            expanded = !expanded;
            updateSummary();
        });
        document.documentElement.appendChild(panel);
    }

    function updateSummary() {
        const live = visibleRecords();
        if (!live.length) { if (panel) panel.remove(); return; }
        ensurePanel();
        const counts = {};
        for (const record of live) counts[record.state] = (counts[record.state] || 0) + 1;
        const completed = live.filter(r => !['queued', 'checking'].includes(r.state)).length;
        const trackerDone = live.reduce((sum, record) => sum + (record.trackerDone || 0), 0);
        const trackerTotal = live.reduce((sum, record) => sum + (record.trackerTotal || 0), 0);
        const dhtRecords = live.filter(record => record.dht && record.dht.status !== 'disabled');
        const dhtDone = dhtRecords.filter(record => record.dht.done).length;
        const label = `Magnet ${completed}/${live.length} · Tracker ${trackerDone}/${trackerTotal}${dhtRecords.length ? ` · DHT ${dhtDone}/${dhtRecords.length}` : ''} · 优质 ${counts.good || 0} ${expanded ? '▾' : '▸'}`;
        if (summaryButton.textContent !== label) summaryButton.textContent = label;
        summaryButton.setAttribute('aria-expanded', String(expanded));
        details.hidden = !expanded;
        if (!expanded) return;
        details.replaceChildren();
        const retry = document.createElement('button');
        retry.id = 'retry'; retry.textContent = '重新检测'; retry.disabled = busy;
        retry.addEventListener('click', () => {
            for (const record of visibleRecords()) {
                if (record.state !== 'checking') {
                    record.trackerDone = 0; record.trackerTotal = 0; record.dht = null;
                    setStatus(record, 'queued', '等待检测', '…');
                }
            }
            updateSummary(); schedulePost();
        });
        details.appendChild(retry);
        const heading = document.createElement('strong');
        heading.textContent = '本页资源汇总'; details.appendChild(heading);
        const stats = document.createElement('div'); stats.className = 'stats';
        for (const [name, count] of [['优质', counts.good || 0], ['有种子', (counts.good || 0) + (counts.seeded || 0)],
            ['仅下载者', counts.peers || 0], ['未发现', counts.empty || 0],
            ['DHT发现Peer的资源', live.filter(r => r.dht?.peer_count > 0).length], ['无响应', counts.unknown || 0], ['错误', counts.error || 0]]) {
            const span = document.createElement('span'); span.textContent = `${name} ${count}`; stats.appendChild(span);
        }
        details.appendChild(stats);
        const note = document.createElement('div'); note.className = 'note';
        note.textContent = `按唯一 BTIH 统计。优质：Tracker 报告 Seed ≥ ${GOOD_SEEDS}；人数取各 Tracker 最大值，不累加。DHT Peer 按资源内 IP+端口去重，Seed 估算单独显示。Tracker 进度按已启动的资源×Tracker 次数计数，待检 ${counts.queued || 0} 个。`;
        details.appendChild(note);
        for (const record of live) {
            const row = document.createElement('div'); row.className = 'row';
            const title = document.createElement('div'); title.className = 'title';
            title.textContent = record.title || record.info_hash; title.title = record.info_hash;
            const status = document.createElement('div'); status.className = 'status';
            status.textContent = record.description; status.style.color = colors[record.state];
            row.append(title, status);
            row.addEventListener('click', () => [...record.elements][0]?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
            details.appendChild(row);
        }
    }

    function paint(anchor, record) {
        const entry = links.get(anchor);
        if (!entry) return;
        let badge = entry.badge;
        if (!badge?.isConnected || badge.previousElementSibling !== anchor) {
            badge?.remove();
            badge = document.createElement('span');
            badge.dataset.magnetProbeUi = 'badge';
            badge.style.cssText = 'display:inline-flex!important;align-items:center!important;vertical-align:middle!important;white-space:nowrap!important;margin:0 0 0 5px!important;padding:1px 5px!important;border-radius:4px!important;font:11px/1.4 system-ui,sans-serif!important;cursor:pointer!important;background:#f1f5f9!important;';
            badge.addEventListener('click', () => { expanded = true; updateSummary(); });
            anchor.after(badge);
            entry.badge = badge;
        }
        if (badge.textContent !== record.short) badge.textContent = record.short;
        badge.title = record.description;
        badge.style.setProperty('color', colors[record.state], 'important');
    }

    function setStatus(record, state, description, short) {
        Object.assign(record, { state, description, short });
        for (const anchor of record.elements) if (anchor.isConnected) paint(anchor, record);
    }

    function scanPage() {
        for (const [anchor, entry] of links) {
            if (!anchor.isConnected || parse(anchor)?.info_hash !== entry.hash) {
                records.get(entry.hash)?.elements.delete(anchor);
                entry.badge?.remove(); links.delete(anchor);
            }
        }
        for (const anchor of document.querySelectorAll('a[href^="magnet:" i]')) {
            const item = parse(anchor);
            if (!item) continue;
            let record = records.get(item.info_hash);
            if (!record) {
                record = { ...item, elements: new Set(), state: 'queued', description: '等待检测', short: '…' };
                records.set(item.info_hash, record);
            }
            record.trackers = [...new Set([...record.trackers, ...item.trackers])];
            record.elements.add(anchor);
            if (!links.has(anchor)) links.set(anchor, { hash: item.info_hash, badge: null });
            paint(anchor, record);
        }
        updateSummary();
        if (visibleRecords().some(r => r.state === 'queued')) schedulePost();
    }

    function schedulePost() {
        clearTimeout(postTimer);
        postTimer = setTimeout(postNewMagnets, 500);
    }

    function renderResult(record, result) {
        const values = ['max_seeders', 'max_leechers', 'active_trackers', 'trackers_tested', 'trackers_responded', 'trackers_completed'];
        if (values.some(key => !Number.isInteger(result[key]) || result[key] < 0)) {
            setStatus(record, 'error', 'API 返回格式错误', '!'); return false;
        }
        if (typeof result.done !== 'boolean' || result.trackers_completed > result.trackers_tested) {
            setStatus(record, 'error', 'API 返回格式错误', '!'); return false;
        }
        const dht = result.dht;
        if (dht && (typeof dht.done !== 'boolean' ||
            ['peer_count', 'queries_completed', 'queries_sent', 'nodes_responded'].some(key => !Number.isInteger(dht[key]) || dht[key] < 0) ||
            (dht.seeders_estimate !== null && (!Number.isInteger(dht.seeders_estimate) || dht.seeders_estimate < 0)))) {
            setStatus(record, 'error', 'API 返回无效 DHT 数据', '!'); return false;
        }
        record.dht = dht || null;
        record.trackerDone = result.trackers_completed;
        record.trackerTotal = result.trackers_tested;
        const seeds = result.max_seeders, peers = result.max_leechers;
        let description = `Seed ≥ ${seeds} | Leecher ≥ ${peers} | 活跃 Tracker ${result.active_trackers}/${result.trackers_tested} | 响应 ${result.trackers_responded}`;
        if (dht && dht.status !== 'disabled') {
            const state = { queued: '等待', checking: '检测中', ok: '完成', no_response: '无响应', error: '错误' }[dht.status] || dht.status;
            description += ` | DHT ${dht.peer_count} Peer · Seed ${dht.seeders_estimate === null ? '未知' : `≈ ${dht.seeders_estimate}`} · 查询 ${dht.queries_completed}/${dht.queries_sent} · 响应 ${dht.nodes_responded} · ${state}`;
            if (dht.error) description += ` (${dht.error})`;
        }
        if (!result.done) {
            setStatus(record, 'checking', `检测中 ${result.trackers_completed}/${result.trackers_tested} | 暂报 ${description}`,
                `… ${result.trackers_completed === result.trackers_tested && dht && !dht.done ? 'DHT' : `${result.trackers_completed}/${result.trackers_tested}`}${seeds ? ` · S${seeds}` : ''}`);
        } else if (seeds) setStatus(record, seeds >= GOOD_SEEDS ? 'good' : 'seeded', description, `● ${seeds}`);
        else if (peers) setStatus(record, 'peers', description, `◐ ${peers}`);
        else if (dht?.peer_count > 0 || dht?.seeders_estimate > 0) setStatus(record, 'dhtpeers', description, dht.peer_count ? `◇ D${dht.peer_count}` : `≈ S${dht.seeders_estimate}`);
        else if (result.trackers_responded) setStatus(record, 'empty', `当前未发现 Seed/Peer | ${description}`, '○ 0');
        else setStatus(record, 'unknown', `Tracker 无有效响应 (${result.trackers_tested})，资源可用性未知`, '?');
        return true;
    }

    function postNewMagnets() {
        if (busy) return;
        const batch = visibleRecords().filter(r => r.state === 'queued').slice(0, BATCH_SIZE);
        if (!batch.length) return;
        busy = true;
        for (const record of batch) setStatus(record, 'checking', '正在检测 Tracker…', '…');
        updateSummary();
        let finished = false, jobId, apiBase = API_URL;
        const started = Date.now();
        function fail(error) {
            if (finished) return;
            finished = true;
            for (const record of batch) {
                if (record.state === 'checking') setStatus(record, 'error', error, '!');
            }
            busy = false; updateSummary(); schedulePost();
        }
        function apply(response, creating) {
            if (finished) return;
            if (response.status < 200 || response.status >= 300) return fail(`API HTTP ${response.status}`);
            let data;
            try { data = JSON.parse(response.responseText); }
            catch { return fail('API 返回无效 JSON'); }
            if (!Array.isArray(data?.results) || typeof data.done !== 'boolean' ||
                typeof data.job_id !== 'string' || !/^[a-f0-9]{32}$/.test(data.job_id)) {
                return fail('API 返回格式错误');
            }
            if (creating) jobId = data.job_id;
            else if (data.job_id !== jobId) return fail('API 返回任务不匹配');
            if (data.error) return fail(`检测失败：${data.error}`);
            for (const record of batch) {
                const result = data.results.find(r => r && typeof r.info_hash === 'string' && canonicalHash(r.info_hash) === record.info_hash);
                if (!result) return fail('API 未返回检测结果');
                if (!renderResult(record, result)) return fail('API 返回格式错误');
            }
            updateSummary();
            if (data.done) {
                for (const record of batch) {
                    if (record.state === 'checking') setStatus(record, 'error', 'API 提前结束检测', '!');
                }
                finished = true; busy = false; updateSummary(); schedulePost();
            } else if (Date.now() - started > 300000) fail('检测超过 5 分钟，请重试');
            else setTimeout(() => request(false), 500);
        }
        function connectionError(creating, message) {
            if (creating && apiBase !== FALLBACK_API_URL) {
                apiBase = FALLBACK_API_URL; request(true);
            } else fail(message);
        }
        function request(creating) {
            try {
                GM_xmlhttpRequest({
                    method: creating ? 'POST' : 'GET', url: creating ? apiBase : `${apiBase}/${jobId}`,
                    headers: creating ? { 'Content-Type': 'application/json' } : {},
                    data: creating ? JSON.stringify({ schema: 'magnet-probe/v3', page_title: document.title,
                        source_url: location.href, enable_dht: ENABLE_DHT, items: batch.map(({ info_hash, magnet, title, trackers, source_url }) =>
                            ({ info_hash, magnet, title, trackers, source_url })) }) : undefined,
                    timeout: 10000,
                    onload: response => apply(response, creating),
                    onerror: () => connectionError(creating, '无法连接 Python API，请检查服务、hosts 和油猴权限'),
                    ontimeout: () => connectionError(creating, '读取检测进度超时，请重试'),
                    onabort: () => fail('检测请求已取消'),
                });
            } catch (error) { fail(`请求失败：${error.message}`); }
        }
        request(true);
    }

    // Remove tags and line breaks left by older versions.
    for (const badge of document.querySelectorAll('[data-magnet-probe-hash]')) {
        if (badge.nextElementSibling?.tagName === 'BR') badge.nextElementSibling.remove();
        badge.remove();
    }
    scanPage();
    const owned = node => node.nodeType === 1 && !!node.closest('[data-magnet-probe-ui]');
    const observer = new MutationObserver(mutations => {
        const relevant = mutations.some(mutation => {
            if (owned(mutation.target)) return false;
            if (mutation.type === 'attributes') return true;
            return [...mutation.addedNodes, ...mutation.removedNodes].some(node => !owned(node));
        });
        if (relevant) {
            clearTimeout(scanTimer);
            scanTimer = setTimeout(scanPage, 250);
        }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['href'] });
})();
