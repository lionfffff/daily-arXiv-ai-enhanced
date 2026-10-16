const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load(fetch) {
  const context = vm.createContext({ URL, Blob, fetch });
  vm.runInContext(fs.readFileSync(require.resolve('../paper-download.js'), 'utf8'), context);
  return context;
}

test('downloads the displayed revision even if an older stored link is unversioned', () => {
  const api = load();
  assert.equal(api.getPaperPdfUrl({ version_id: '2610.17856v2', pdf: 'http://arxiv.org/pdf/2610.17856' }), 'https://arxiv.org/pdf/2610.17856v2');
  assert.equal(api.getPaperPdfUrl({ url: 'https://arxiv.org/abs/cs/0601001v3' }), 'https://arxiv.org/pdf/cs/0601001v3');
  assert.throws(() => api.getPaperPdfUrl({ url: 'javascript:alert(1)' }));
});

test('validates actual PDF bytes and passes cancellation to fetch', async () => {
  const signal = new AbortController().signal;
  const api = load(async (url, options) => {
    assert.equal(options.signal, signal);
    assert.equal(options.credentials, 'omit');
    return { ok: true, blob: async () => new Blob(['%PDF-1.7\nexample']) };
  });
  const result = await api.fetchPaperPdf('https://arxiv.org/pdf/2610.17856v2', signal);
  assert.equal(result.type, 'application/pdf');
  assert.match(await result.text(), /^%PDF-/);
});

test('does not save an HTTP error or HTML error page with a PDF filename', async () => {
  await assert.rejects(load(async () => ({ ok: false, status: 429 })).fetchPaperPdf('https://arxiv.org/pdf/2610.17856v2'), /429/);
  await assert.rejects(load(async () => ({ ok: true, blob: async () => new Blob(['<html>Unavailable</html>']) })).fetchPaperPdf('https://arxiv.org/pdf/2610.17856v2'), /did not return a PDF/);
});
