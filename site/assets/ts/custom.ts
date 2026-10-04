// Stack's custom-script extension point. No analytics requests on local previews.
const settings = document.querySelector<HTMLTemplateElement>('#article-view-counter');
if (settings) {
    const details = document.querySelector('.main-article .article-details');
    if (details) {
        let footer = details.querySelector<HTMLElement>('.article-meta');
        if (!footer) {
            footer = document.createElement('footer');
            footer.className = 'article-meta';
            details.appendChild(footer);
        }
        const label = document.createElement('span');
        label.className = 'inline-meta article-views';
        label.title = '页面访问次数，重复访问也会计数，不代表读完人数';
        const value = document.createElement('span');
        label.append('阅读量：', value);
        footer.appendChild(label);

        const allowed = location.protocol === 'https:' &&
            location.hostname === settings.dataset.host && location.port === '';
        value.textContent = allowed ? '加载中…' : '上线后统计';
        if (allowed) {
            // Stable path only: title, query, heading fragment and incoming referrer
            // are not transmitted or used to split this article's count.
            const canonical = new URL(settings.dataset.path!, location.origin);
            canonical.search = '';
            canonical.hash = '';
            fetch('https://cdn.busuanzi.cc/api.php', {
                method: 'POST',
                body: JSON.stringify({ url: canonical.href, referrer: '' }),
                credentials: 'omit',
                referrerPolicy: 'no-referrer',
                signal: AbortSignal.timeout(8000),
            }).then(response => {
                if (!response.ok) throw new Error('Counter unavailable');
                return response.json();
            }).then(data => {
                const count = String(data.busuanzi_page_pv ?? '');
                if (!/^\d+$/.test(count)) throw new Error('Invalid counter');
                value.textContent = Number(count).toLocaleString('zh-CN') + ' 次';
            }).catch(() => { value.textContent = '暂不可用'; });
        }
    }
}
