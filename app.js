const $ = (id) => document.getElementById(id);
let allProducts = [];

function formatMoney(value, currency) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'AUD', maximumFractionDigits: 2 }).format(Number(value)); }
  catch { return `${currency || ''} ${Number(value).toFixed(2)}`.trim(); }
}
function formatDate(value) {
  if (!value) return 'Never checked';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown date' : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function alertThreshold(p) {
  if (p.target_price != null) return Number(p.target_price);
  if (p.initial_price == null) return null;
  return Number(p.initial_price) * (1 - Number(p.drop_percent || 10) / 100);
}
function text(tag, className, value) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = value;
  return el;
}
function makeHistory(points) {
  const wrap = text('div', 'history', '');
  wrap.setAttribute('aria-label', 'Recent price history');
  const values = (points || []).slice(-12).map(x => Number(x.price)).filter(Number.isFinite);
  if (values.length < 2) return wrap;
  const min = Math.min(...values), max = Math.max(...values);
  for (const value of values) {
    const bar = text('span', 'bar', '');
    bar.style.height = `${6 + (max === min ? 12 : (value - min) / (max - min) * 18)}px`;
    bar.setAttribute('aria-hidden', 'true');
    wrap.append(bar);
  }
  return wrap;
}
function card(p) {
  const el = text('article', 'card', '');
  const top = text('div', 'card-top', '');
  top.append(text('span', 'retailer', p.retailer || 'Retailer'));
  const threshold = alertThreshold(p);
  const hit = p.status === 'ok' && p.current_price != null && threshold != null && Number(p.current_price) <= threshold;
  const status = hit ? 'At target' : p.status === 'ok' ? 'Watching' : p.status === 'check_failed' ? 'Check failed' : p.status === 'unavailable' ? 'Unavailable' : 'Price pending';
  top.append(text('span', `status ${hit ? 'hit' : p.status === 'ok' ? '' : 'warn'}`, status));
  el.append(top);
  const h = text('h3', '', '');
  const link = text('a', '', p.name || 'Unnamed product');
  try {
    const url = new URL(p.url);
    if (['http:', 'https:'].includes(url.protocol)) { link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
  } catch {}
  h.append(link); el.append(h);
  const row = text('div', 'price-row', '');
  const current = text('div', 'price-block', '');
  current.append(text('small', '', 'Current price'), text('strong', 'price', formatMoney(p.current_price, p.currency)));
  const target = text('div', 'target-block', '');
  target.append(text('small', '', p.target_price != null ? 'Your target' : 'Drop alert at'),
                text('span', 'target', formatMoney(threshold, p.currency)));
  row.append(current, target); el.append(row);
  const foot = text('div', 'card-foot', '');
  foot.append(text('span', '', `Requested ${p.postcode || '3820'} · Checked ${formatDate(p.last_checked_at)}`), makeHistory(p.price_history));
  el.append(foot);
  return el;
}
function render() {
  const query = $('filter').value.trim().toLowerCase();
  const visible = allProducts.filter(p => `${p.name || ''} ${p.retailer || ''}`.toLowerCase().includes(query));
  $('products').replaceChildren(...visible.map(card));
  $('message').textContent = !allProducts.length ? 'No products yet. Ask the Hermes agent on Telegram to find one, then tell it to track it.' : !visible.length ? 'No products match your search.' : '';
}
async function load() {
  try {
    const response = await fetch('./data/products.json', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.products)) throw new Error('Unexpected data format');
    allProducts = data.products;
    $('postcode').textContent = data.settings?.postcode || '3820';
    $('count').textContent = String(allProducts.length).padStart(2, '0');
    $('at-target').textContent = String(allProducts.filter(p => p.status === 'ok' && p.current_price != null && alertThreshold(p) != null && Number(p.current_price) <= alertThreshold(p)).length).padStart(2, '0');
    const dates = allProducts.map(p => new Date(p.last_checked_at).getTime()).filter(Number.isFinite);
    $('updated').textContent = dates.length ? formatDate(new Date(Math.max(...dates))) : '—';
    render();
  } catch (error) {
    $('message').textContent = `Could not load products: ${error.message}. Try refreshing the page.`;
  }
}
$('filter').addEventListener('input', render);
load();
