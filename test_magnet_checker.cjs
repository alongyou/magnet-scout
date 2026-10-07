const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const playwright = require('playwright');
const script = fs.readFileSync(path.join(__dirname, 'magnet_checker.js'), 'utf8');
const HASH = '0123456789ABCDEF0123456789ABCDEF01234567';
const magnet = hash => `magnet:?xt=urn:btih:${hash}`;
const result = (hash, seeds = 23) => ({ info_hash: hash, max_seeders: seeds, max_leechers: 8,
    active_trackers: 2, trackers_tested: 2, trackers_responded: 2, trackers_completed: 2, done: true });

(async () => {
    const executablePath = process.env.PROBE_TEST_BROWSER || undefined;
    const browser = await playwright.chromium.launch({ headless: true, executablePath, timeout: 15000 });
    try {
        const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.setContent(`<style>body{font:16px system-ui;padding:40px;background:#f8fafc}article{background:white;margin:12px;padding:20px;border:1px solid #e2e8f0;border-radius:10px}</style>
            <h2>Magnet 检测 · 示例页面</h2><article><h3>示例资源 A</h3><div data-magnet-probe-hash="${HASH}">旧标签</div><br><a href="${magnet(HASH)}">磁力链接</a></article>`);
        await page.evaluate(() => { window.requests = []; window.GM_xmlhttpRequest = request => window.requests.push(request); });
        await page.addScriptTag({ content: script });
        await page.waitForFunction(() => window.requests.length === 1);
        assert.equal(await page.locator('br').count(), 0, 'legacy breaks removed');
        assert.equal(await page.locator('[data-magnet-probe-hash]').count(), 0);
        assert.equal(await page.locator('[data-magnet-probe-ui="badge"]').count(), 1);
        assert.equal(await page.locator('[data-magnet-probe-ui="badge"]').evaluate(el => getComputedStyle(el).display), 'inline-flex');
        await page.evaluate(({hash, magnet}) => {
            const a = document.createElement('a'); a.href = magnet; a.textContent = '重复链接'; document.querySelector('article').append(a);
        }, { hash: HASH, magnet: magnet(HASH) });
        await page.waitForFunction(() => document.querySelectorAll('[data-magnet-probe-ui="badge"]').length === 2);
        assert.equal(await page.evaluate(() => window.requests.length), 1, 'no duplicate pending request');
        await page.evaluate(data => window.requests[0].onload({ status: 200, responseText: JSON.stringify({ job_id: 'a'.repeat(32), done: true, results: [data] }) }), result(HASH));
        await page.locator('[data-magnet-probe-ui="panel"]').locator('#summary').click();
        assert.match(await page.locator('[data-magnet-probe-ui="panel"]').locator('#details').innerText(), /按唯一 BTIH/);
        assert.match(await page.locator('[data-magnet-probe-ui="panel"]').locator('#summary').innerText(), /Magnet 1\/1 · Tracker 2\/2 · 优质 1/);
        assert.equal(await page.locator('[data-magnet-probe-ui="panel"]').locator('.row').count(), 1);
        await page.addScriptTag({ content: script });
        assert.equal(await page.locator('[data-magnet-probe-ui="panel"]').count(), 1, 'singleton panel');
        await page.evaluate(magnet => {
            const article = document.createElement('article'); article.innerHTML = `<h3>示例资源 A · 新增链接</h3><a href="${magnet}">磁力链接</a>`;
            document.body.append(article);
        }, magnet(HASH));
        await page.waitForFunction(() => document.querySelectorAll('[data-magnet-probe-ui="badge"]').length === 3);
        assert.deepEqual(await page.locator('[data-magnet-probe-ui="badge"]').allTextContents(), ['● 23', '● 23', '● 23']);
        assert.equal(await page.evaluate(() => window.requests.length), 1);
        await page.screenshot({ path: path.join(__dirname, 'ui-preview.png') });
        // Removing and changing hrefs must update the visible statistics and remove stale badges.
        await page.evaluate(() => { document.querySelectorAll('a').forEach(a => a.href = 'https://example.test/'); });
        await page.waitForFunction(() => !document.querySelector('[data-magnet-probe-ui="panel"]'));
        assert.equal(await page.locator('[data-magnet-probe-ui="badge"]').count(), 0);

        const progressPage = await browser.newPage();
        await progressPage.setContent(`<a href="${magnet(HASH)}">进度测试</a>`);
        await progressPage.evaluate(() => { window.requests = []; window.GM_xmlhttpRequest = request => window.requests.push(request); });
        await progressPage.addScriptTag({ content: script });
        await progressPage.waitForFunction(() => window.requests.length === 1);
        await progressPage.evaluate(data => window.requests[0].onload({ status: 200, responseText: JSON.stringify({
            job_id: 'b'.repeat(32), done: false, results: [{ ...data, done: false, trackers_completed: 1, trackers_responded: 1, active_trackers: 1 }],
        }) }), result(HASH));
        const progressSummary = progressPage.locator('[data-magnet-probe-ui="panel"]').locator('#summary');
        assert.match(await progressSummary.innerText(), /Magnet 0\/1 · Tracker 1\/2/);
        assert.match(await progressPage.locator('[data-magnet-probe-ui="badge"]').innerText(), /1\/2 · S23/);
        await progressPage.waitForFunction(() => window.requests.length === 2);
        assert.equal(await progressPage.evaluate(() => window.requests[1].method), 'GET');
        assert.ok((await progressPage.evaluate(() => window.requests[1].url)).endsWith('b'.repeat(32)));
        await progressPage.evaluate(data => window.requests[1].onload({status: 200, responseText: JSON.stringify({
            job_id: 'b'.repeat(32), done: true, results: [data],
        })}), result(HASH));
        assert.match(await progressSummary.innerText(), /Magnet 1\/1 · Tracker 2\/2 · 优质 1/);
        assert.equal(await progressPage.locator('[data-magnet-probe-ui="badge"]').innerText(), '● 23');
        await progressPage.close();

        const batchPage = await browser.newPage();
        await batchPage.setContent('<h2>Magnet 检测 · 示例页面</h2>');
        await batchPage.evaluate(magnet => {
            for (let i = 0; i < 17; i++) {
                const article = document.createElement('article');
                const a = document.createElement('a'); a.href = magnet.replace(/0123456789ABCDEF0123456789ABCDEF01234567/, i.toString(16).padStart(40, '0')); a.textContent = `资源 ${i + 1}`;
                article.append(a); document.body.append(article);
            }
        }, magnet(HASH));
        await batchPage.evaluate(() => { window.requests = []; window.GM_xmlhttpRequest = request => window.requests.push(request); });
        await batchPage.addScriptTag({ content: script });
        for (let index = 0; index < 3; index++) {
            await batchPage.waitForFunction(n => window.requests.length > n, index);
            const hashes = await batchPage.evaluate(n => JSON.parse(window.requests[n].data).items.map(x => x.info_hash), index);
            assert.ok(hashes.length <= 8);
            await batchPage.evaluate(({n, results}) => window.requests[n].onload({ status: 200, responseText: JSON.stringify({ job_id: 'a'.repeat(32), done: true, results }) }),
                { n: index, results: hashes.map(hash => result(hash)) });
        }
        assert.match(await batchPage.locator('[data-magnet-probe-ui="panel"]').locator('#summary').innerText(), /17\/17/);
        await batchPage.locator('[data-magnet-probe-ui="panel"]').locator('#summary').click();
        await batchPage.locator('[data-magnet-probe-ui="panel"]').locator('#retry').click();
        await batchPage.waitForFunction(() => window.requests.length === 4);
        await batchPage.evaluate(() => window.requests[3].onload({status:200, responseText:'invalid'}));
        assert.ok(await batchPage.locator('[data-magnet-probe-ui="badge"]').filter({hasText:'!'}).count() > 0);
        assert.deepEqual(errors, []);
        await batchPage.close();
        await page.close();
        console.log('PASS: real-browser layout, legacy cleanup, deduplication, summary, href changes, batching, incremental tracker progress, retry and API errors');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
