// Keep the PDF preview and download on the exact arXiv version shown in the card.
function getPaperPdfUrl(paper) {
  const idPattern = /^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7})(?:v\d+)?$/i;
  if (paper.version_id && idPattern.test(paper.version_id)) {
    return `https://arxiv.org/pdf/${paper.version_id}`;
  }
  for (const candidate of [paper.pdf, paper.url]) {
    if (!candidate) continue;
    try {
      const url = new URL(candidate);
      const id = url.pathname.replace(/^\/(?:abs|pdf|html)\//, '').replace(/\.pdf$/, '');
      if ((url.hostname === 'arxiv.org' || url.hostname === 'www.arxiv.org') && idPattern.test(id)) {
        return `https://arxiv.org/pdf/${id}`;
      }
    } catch (_) { /* Try the other stored link or the paper ID. */ }
  }
  if (paper.id && idPattern.test(paper.id)) return `https://arxiv.org/pdf/${paper.id}`;
  throw new Error('Invalid arXiv paper ID');
}

async function fetchPaperPdf(url, signal) {
  const response = await fetch(url, { signal, credentials: 'omit' });
  if (!response.ok) throw new Error(`PDF request failed: HTTP ${response.status}`);
  const blob = await response.blob();
  if (!(await blob.slice(0, 5).text()).startsWith('%PDF-')) {
    throw new Error('The server did not return a PDF');
  }
  return new Blob([blob], { type: 'application/pdf' });
}

function bindPdfDownload(paper) {
  const button = document.getElementById('downloadPdfButton');
  const status = document.getElementById('pdfDownloadStatus');
  button.downloadController?.abort();
  button.downloadController = null;
  button.disabled = false;
  button.removeAttribute('aria-busy');
  button.title = '下载 PDF';
  status.textContent = '';

  const url = getPaperPdfUrl(paper);
  const filename = `${url.split('/pdf/')[1].replace(/\//g, '_')}.pdf`;
  button.onclick = async () => {
    if (button.disabled) return;
    const controller = new AbortController();
    button.downloadController = controller;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.title = '正在下载…';
    status.textContent = '正在下载 PDF…';
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      const blob = await fetchPaperPdf(url, controller.signal);
      if (button.downloadController !== controller) return;
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      status.textContent = '已发起保存，请查看浏览器下载列表。';
    } catch (error) {
      if (button.downloadController === controller) {
        status.textContent = '下载失败，请重试，或点击旁边的 PDF 图标打开后保存。';
        console.error('PDF download failed:', error);
      }
    } finally {
      clearTimeout(timeout);
      if (button.downloadController === controller) {
        button.downloadController = null;
        button.disabled = false;
        button.removeAttribute('aria-busy');
        button.title = '下载 PDF';
      }
    }
  };
}
