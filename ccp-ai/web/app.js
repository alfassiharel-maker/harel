/* CCP-AI console.

   No framework and no innerHTML for anything that came from the server: every
   value is written with textContent. The API only ever returns our own data
   today, but a console that displays model names from other publishers must not
   have an injection path waiting in it.

   The UI holds no derived numbers of its own. Percentages, dollars and byte
   counts are rendered from what /api/state measured; the browser formats, it does
   not compute savings. */

const STATE = {
  state: null,
  lang: 'en',
  polling: null,
  lastInspected: null,
};

/* ── i18n ──────────────────────────────────────────────────────────────── */

const STRINGS = {
  en: {
    brandTitle: 'CCP-AI',
    brandTagline: 'Lossless base + delta storage for model weights',
    switchTitle: 'CCP savings',
    switchOn: 'ON — variants stored as verified deltas',
    switchOff: 'OFF — full checkpoints on disk',
    switchWorking: 'migrating bytes on disk…',
    switchNote: 'Flipping this migrates real bytes on disk. Off writes full checkpoints back out; on re-encodes them as verified deltas. Nothing here is a display setting.',
    on: 'ON', off: 'OFF',
    onDisk: 'On disk now',
    registryBaseline: 'Registry baseline',
    fleetSaving: 'Fleet saving (measured)',
    losslessLabel: 'Reconstruction',
    notChecked: 'not checked',
    lossless: 'bit-exact',
    notLossless: 'FAILED',
    barFull: 'full copies',
    barCcp: 'CCP deltas',
    seedBtn: 'Seed model family',
    verifyBtn: 'Verify losslessness',
    resetBtn: 'Reset',
    variantsTitle: 'Model family',
    variantsSub: 'Every row is a real checkpoint on disk. Savings are measured per variant, never modelled.',
    thVariant: 'Variant', thKind: 'Kind', thRaw: 'Full size', thGzip: 'gzip -6', thCcp: 'CCP',
    thSaving: 'Saving', thVsGzip: 'vs gzip', thComposition: 'Composition',
    noVariants: 'Nothing stored yet — seed the model family.',
    inspect: 'Inspect',
    projTitle: 'Projection to production scale',
    projSub: 'The measured per-variant ratio above, applied to a fleet you choose. This is an extrapolation — the ratio is real, the fleet is hypothetical, and the unit prices are AWS S3 list.',
    projParams: 'Model size (B params)',
    projVariants: 'Variants published',
    projDownloads: 'Downloads per variant',
    projBtn: 'Recalculate',
    projEgress: 'Egress — one release cycle',
    projStorage: 'Storage — per month',
    projToday: 'Today', projWithCcp: 'With CCP', projSaved: 'Saved',
    projAssumptions: 'Assumptions',
    mechTitle: 'Why it compresses',
    mechSub: 'A fine-tune moves a weight by a fraction of its value. In IEEE-754 that leaves the sign, the exponent and the top mantissa bits untouched. Split the residual into byte planes and the redundancy becomes visible — and compressible. Below: the actual planes of the largest changed tensor in the selected variant.',
    planeEmpty: 'Inspect a variant to see its byte planes.',
    verifyTitle: 'Lossless verification',
    verifySub: 'Each variant is rebuilt from its container and hashed against the digest recorded when it was pushed. Bit-exact or it fails.',
    verifyEmpty: 'Not run yet.',
    ledgerTitle: 'Operation ledger',
    ledgerSub: 'Append-only and hash-chained. Every saving shown above traces to an entry here.',
    footer: 'Numbers on this page come from os.stat on real files and from containers that were built and verified in process. Figures labelled projection apply a measured ratio to a hypothetical fleet.',
    seeding: 'generating real checkpoints — this is real arithmetic, not a spinner',
    seedFirst: 'Seed the family first.',
    migrated: (n, before, after, secs) => `migrated ${n} variants in ${secs}s · ${before} → ${after}`,
    verifiedAll: (n, secs) => `${n} variants rebuilt and hash-matched in ${secs}s`,
    verifyFailed: 'one or more variants did not rebuild bit-exactly',
    chainOk: 'ledger chain verified',
    chainBad: 'ledger chain broken',
    perTensor: 'Per-tensor plan',
    composition: 'Composition',
    planes: 'Byte planes of the largest changed tensor',
    copied: 'copied from base (0 bytes)',
    deltaed: 'delta encoded',
    whole: 'stored whole',
    thTensor: 'Tensor', thShape: 'Shape', thOp: 'Op', thStored: 'Stored', thRatio: 'Ratio',
    zeroFraction: 'zero bytes',
    langButton: 'עברית',
  },
  he: {
    brandTitle: 'CCP-AI',
    brandTagline: 'אחסון דחוס ללא אובדן: מודל בסיס + דלתות',
    switchTitle: 'חיסכון CCP',
    switchOn: 'פועל — הווריאנטים נשמרים כדלתות מאומתות',
    switchOff: 'כבוי — מודלים מלאים על הדיסק',
    switchWorking: 'מעביר בייטים בדיסק…',
    switchNote: 'הלחיצה מזיזה בייטים אמיתיים על הדיסק. כיבוי כותב בחזרה מודלים מלאים, הפעלה מקודדת אותם כדלתות מאומתות. זו אינה הגדרת תצוגה.',
    on: 'פועל', off: 'כבוי',
    onDisk: 'נפח בדיסק כרגע',
    registryBaseline: 'בסיס להשוואה (אחסון רגיל)',
    fleetSaving: 'חיסכון נמדד',
    losslessLabel: 'שחזור',
    notChecked: 'לא נבדק',
    lossless: 'זהה ביט-לביט',
    notLossless: 'נכשל',
    barFull: 'עותקים מלאים',
    barCcp: 'דלתות CCP',
    seedBtn: 'צור משפחת מודלים',
    verifyBtn: 'אמת שחזור מלא',
    resetBtn: 'איפוס',
    variantsTitle: 'משפחת המודלים',
    variantsSub: 'כל שורה היא מודל אמיתי על הדיסק. החיסכון נמדד לכל וריאנט, לא מוערך.',
    thVariant: 'וריאנט', thKind: 'סוג', thRaw: 'גודל מלא', thGzip: 'gzip -6', thCcp: 'CCP',
    thSaving: 'חיסכון', thVsGzip: 'מול gzip', thComposition: 'הרכב',
    noVariants: 'אין עדיין נתונים — צור את משפחת המודלים.',
    inspect: 'פירוט',
    projTitle: 'הרחבה לקנה מידה תעשייתי',
    projSub: 'היחס שנמדד למעלה, מוחל על צי מודלים לבחירתך. זו הרחבה: היחס אמיתי, הצי היפותטי, והמחירים הם מחירון AWS S3.',
    projParams: 'גודל מודל (מיליארדי פרמטרים)',
    projVariants: 'מספר וריאנטים',
    projDownloads: 'הורדות לכל וריאנט',
    projBtn: 'חשב מחדש',
    projEgress: 'תעבורה — מחזור הפצה אחד',
    projStorage: 'אחסון — לחודש',
    projToday: 'היום', projWithCcp: 'עם CCP', projSaved: 'חיסכון',
    projAssumptions: 'הנחות',
    mechTitle: 'למה זה נדחס',
    mechSub: 'כיוונון עדין מזיז משקל בשבריר מערכו. בייצוג IEEE-754 הסימן, המעריך והביטים הגבוהים של המנטיסה נשארים זהים. פיצול השארית למישורי בייטים חושף את הכפילות — וממנה בא החיסכון. למטה: המישורים האמיתיים של הטנזור הגדול שהשתנה.',
    planeEmpty: 'לחץ "פירוט" על וריאנט כדי לראות את מישורי הבייטים.',
    verifyTitle: 'אימות ללא אובדן',
    verifySub: 'כל וריאנט משוחזר מהמכולה ומושווה ל-hash שנרשם בעת הדחיסה. זהה ביט-לביט, או שהבדיקה נכשלת.',
    verifyEmpty: 'עדיין לא הורץ.',
    ledgerTitle: 'יומן פעולות',
    ledgerSub: 'לוג לצירוף בלבד, משורשר ב-hash. כל חיסכון שמוצג נובע מרשומה כאן.',
    footer: 'המספרים בדף נמדדו ב-os.stat על קבצים אמיתיים וממכולות שנבנו ואומתו בתהליך. מספרים המסומנים כהרחבה מחילים יחס נמדד על צי היפותטי.',
    seeding: 'מייצר מודלים אמיתיים — חשבון אמיתי, לא אנימציה',
    seedFirst: 'צור קודם את משפחת המודלים.',
    migrated: (n, before, after, secs) => `הועברו ${n} וריאנטים ב-${secs} שניות · ${before} → ${after}`,
    verifiedAll: (n, secs) => `${n} וריאנטים שוחזרו והותאמו ב-${secs} שניות`,
    verifyFailed: 'לפחות וריאנט אחד לא שוחזר בצורה מדויקת',
    chainOk: 'שרשרת היומן אומתה',
    chainBad: 'שרשרת היומן שבורה',
    perTensor: 'תוכנית לפי טנזור',
    composition: 'הרכב',
    planes: 'מישורי בייטים של הטנזור הגדול שהשתנה',
    copied: 'הועתק מהבסיס (0 בייט)',
    deltaed: 'קודד כדלתא',
    whole: 'נשמר במלואו',
    thTensor: 'טנזור', thShape: 'צורה', thOp: 'פעולה', thStored: 'נשמר', thRatio: 'יחס',
    zeroFraction: 'בייטים אפס',
    langButton: 'English',
  },
};

const t = (key) => STRINGS[STATE.lang][key] ?? STRINGS.en[key] ?? key;

function applyLanguage() {
  const html = document.documentElement;
  html.lang = STATE.lang;
  html.dir = STATE.lang === 'he' ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    const value = STRINGS[STATE.lang][node.dataset.i18n];
    if (typeof value === 'string') node.textContent = value;
  });
  $('langToggle').textContent = t('langButton');
  if (STATE.state) render(STATE.state);
  if (STATE.lastInspected) renderPlanes(STATE.lastInspected);
}

/* ── helpers ───────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

function fmtBytes(n) {
  if (n === null || n === undefined) return '—';
  let value = Number(n);
  let unit = 0;
  while (Math.abs(value) >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : value >= 100 ? 1 : 2;
  return `${value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${UNITS[unit]}`;
}

function fmtUsd(n) {
  if (n === null || n === undefined) return '—';
  const value = Number(n);
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 1000) return `$${(value / 1000).toFixed(1)}K`;
  // At demo scale the monthly bill is fractions of a cent. Rounding it to $0.00
  // makes a working measurement look like a broken one, so small values keep
  // their significant digits.
  if (value !== 0 && Math.abs(value) < 0.01) return `$${value.toFixed(5)}`;
  return `$${value.toFixed(2)}`;
}

function fmtPct(ratio, digits = 1) {
  if (ratio === null || ratio === undefined) return '—';
  return `${(Number(ratio) * 100).toFixed(digits)}%`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', 'X-CCP-Tenant': 'demo' },
    ...options,
  });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function note(message, isError = false) {
  const node = $('actionNote');
  node.textContent = message || '';
  node.style.color = isError ? 'var(--danger)' : 'var(--muted)';
}

/* ── rendering ─────────────────────────────────────────────────────────── */

function render(state) {
  STATE.state = state;
  const repo = state.repository;
  const on = state.ccp_enabled;

  $('enginePill').textContent = `engine ${state.engine_version}`;
  $('tenantPill').textContent = `tenant ${state.tenant_id}`;

  const sw = $('ccpSwitch');
  sw.setAttribute('aria-checked', String(on));
  $('switchState').textContent = on ? t('switchOn') : t('switchOff');

  $('diskNow').textContent = fmtBytes(repo.disk_bytes);
  $('diskNowCost').textContent = `${fmtUsd(state.costs.disk_usd_month_now)} / month at S3 list`;
  $('diskFull').textContent = fmtBytes(repo.disk_bytes_if_full);
  $('diskFullCost').textContent = `${fmtUsd(state.costs.disk_usd_month_if_full)} / month at S3 list`;

  const saving = repo.variant_savings_ratio;
  $('fleetSaving').textContent = fmtPct(saving);
  const savedBytes = repo.disk_bytes_if_full - repo.disk_bytes_if_ccp;
  $('fleetSavingBytes').textContent = repo.variant_count
    ? `${fmtBytes(savedBytes)} of ${fmtBytes(repo.disk_bytes_if_full)}`
    : '—';

  const maxBytes = Math.max(repo.disk_bytes_if_full || 1, repo.disk_bytes_if_ccp || 1);
  $('barFull').style.width = `${((repo.disk_bytes_if_full || 0) / maxBytes) * 100}%`;
  $('barCcp').style.width = `${((repo.disk_bytes_if_ccp || 0) / maxBytes) * 100}%`;
  $('barFullValue').textContent = fmtBytes(repo.disk_bytes_if_full);
  $('barCcpValue').textContent = fmtBytes(repo.disk_bytes_if_ccp);

  renderVariants(repo.variants || []);
  renderLegend();
  renderProjection(state.projection);
  renderJob(state.job);
  loadLedger();
}

function renderVariants(variants) {
  const body = $('variantsBody');
  clear(body);

  if (!variants.length) {
    const row = el('tr', 'empty');
    row.appendChild(el('td', null, t('noVariants'))).colSpan = 9;
    body.appendChild(row);
    return;
  }

  for (const v of variants) {
    const row = el('tr');

    const nameCell = el('td');
    const wrap = el('div', 'variant-name');
    wrap.appendChild(el('span', null, v.label));
    wrap.appendChild(el('small', null, v.variant_id));
    nameCell.appendChild(wrap);
    row.appendChild(nameCell);

    const kindCell = el('td');
    kindCell.appendChild(el('span', 'kind', v.kind));
    row.appendChild(kindCell);

    row.appendChild(el('td', 'num', fmtBytes(v.raw_bytes)));
    row.appendChild(el('td', 'num', fmtBytes(v.gzip_bytes)));
    row.appendChild(el('td', 'num', fmtBytes(v.container_bytes)));
    row.appendChild(el('td', 'num saving', fmtPct(v.savings_ratio)));
    row.appendChild(el('td', 'num', fmtPct(v.savings_ratio_vs_gzip)));

    row.appendChild(compositionCell(v));

    const actionCell = el('td');
    const button = el('button', 'ghost', t('inspect'));
    button.type = 'button';
    button.addEventListener('click', () => inspect(v.variant_id));
    actionCell.appendChild(button);
    row.appendChild(actionCell);

    body.appendChild(row);
  }
}

function compositionCell(variant) {
  const cell = el('td');
  const bytes = variant.pack_stats && variant.pack_stats.bytes;
  if (!bytes) return cell;

  const copied = bytes.identical_to_base || 0;
  const delta = bytes.delta_raw || 0;
  const whole = bytes.literal_raw || 0;
  const total = copied + delta + whole || 1;

  const bar = el('div', 'composition');
  bar.title = `${t('copied')}: ${fmtBytes(copied)} · ${t('deltaed')}: ${fmtBytes(delta)} · ${t('whole')}: ${fmtBytes(whole)}`;
  for (const [cls, value] of [['comp-copy', copied], ['comp-delta', delta], ['comp-whole', whole]]) {
    if (value <= 0) continue;
    const seg = el('span', cls);
    seg.style.width = `${(value / total) * 100}%`;
    bar.appendChild(seg);
  }
  cell.appendChild(bar);
  return cell;
}

function renderLegend() {
  const host = $('compositionLegend');
  clear(host);
  for (const [cls, key] of [['comp-copy', 'copied'], ['comp-delta', 'deltaed'], ['comp-whole', 'whole']]) {
    const item = el('span');
    const swatch = el('i', cls);
    item.appendChild(swatch);
    item.appendChild(el('span', null, t(key)));
    host.appendChild(item);
  }
}

function renderProjection(projection) {
  if (!projection) return;
  const a = projection.assumptions;

  $('egressBase').textContent = fmtUsd(projection.egress.baseline_usd);
  $('egressCcp').textContent = fmtUsd(projection.egress.ccp_usd);
  $('egressSaved').textContent = `${fmtUsd(projection.egress.saved_usd)} · ${fmtPct(projection.savings_ratio)}`;
  $('egressBytes').textContent = `${fmtBytes(projection.egress.baseline_bytes)} → ${fmtBytes(projection.egress.ccp_bytes)}`;

  $('storeBase').textContent = fmtUsd(projection.storage.baseline_usd_month);
  $('storeCcp').textContent = fmtUsd(projection.storage.ccp_usd_month);
  $('storeSaved').textContent = `${fmtUsd(projection.storage.saved_usd_month)} / mo · ${fmtUsd(projection.storage.saved_usd_year)} / yr`;
  $('storeBytes').textContent = `${fmtBytes(projection.storage.baseline_bytes)} → ${fmtBytes(projection.storage.ccp_bytes)}`;

  const list = $('projAssumptions');
  clear(list);
  const rows = [
    `${a.params_billions}B params × ${a.bytes_per_param} bytes = ${fmtBytes(a.model_bytes)} per checkpoint`,
    `${a.variants} variants × ${a.downloads_per_variant.toLocaleString()} downloads`,
    `measured delta ratio ${fmtPct(a.measured_variant_ratio, 2)} of full size`,
    `egress $${a.egress_usd_per_gib}/GiB · storage $${a.storage_usd_per_gib_month}/GiB-month`,
    'base fetched once per client, then one delta per variant',
  ];
  for (const text of rows) list.appendChild(el('li', null, text));
}

function renderJob(job) {
  const box = $('jobBox');
  if (!job || job.state === 'idle') {
    box.classList.add('hidden');
    return;
  }
  // A job that finished a while ago is history, not progress. Without this a
  // language switch or a poll would resurrect the panel of a completed seed.
  const settled = job.state !== 'running';
  const age = job.finished_unix ? Date.now() / 1000 - job.finished_unix : 0;
  if (settled && age > 12) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  $('jobMessage').textContent = job.error ? job.error : job.message || job.state;
  $('jobPercent').textContent = `${Math.round((job.fraction || 0) * 100)}% · ${job.elapsed_seconds}s`;
  $('jobBar').style.width = `${(job.fraction || 0) * 100}%`;

  const steps = $('jobSteps');
  clear(steps);
  for (const step of job.steps || []) steps.appendChild(el('li', null, step));

  if (job.state === 'done' || job.state === 'failed') {
    setTimeout(() => box.classList.add('hidden'), job.state === 'failed' ? 12000 : 3500);
  }
}

function renderPlanes(detail) {
  const box = $('planeBox');
  clear(box);
  const planes = detail.plane_report;
  if (!planes || !planes.length) {
    box.appendChild(el('p', 'muted', t('planeEmpty')));
    return;
  }

  const caption = el('p', 'plane-caption', `${detail.label} · ${planes[0].tensor}`);
  box.appendChild(caption);

  const maxStored = Math.max(...planes.map((p) => p.stored_bytes)) || 1;
  for (const plane of [...planes].reverse()) {
    const row = el('div', 'plane-row');
    row.appendChild(el('span', 'plane-label', `byte ${plane.plane} · ${plane.role}`));

    const track = el('div', 'plane-track');
    const share = plane.stored_bytes / plane.raw_bytes;
    const fill = el('div', share > 0.5 ? 'plane-fill dense' : 'plane-fill');
    fill.style.width = `${(plane.stored_bytes / maxStored) * 100}%`;
    track.appendChild(fill);
    row.appendChild(track);

    const zero = plane.zero_fraction === null ? '—' : fmtPct(plane.zero_fraction, 0);
    row.appendChild(el('span', 'plane-value', `${fmtBytes(plane.stored_bytes)} / ${fmtBytes(plane.raw_bytes)} · ${zero} ${t('zeroFraction')}`));
    box.appendChild(row);
  }
}

/* ── actions ───────────────────────────────────────────────────────────── */

async function refresh() {
  try {
    const state = await api('/api/state');
    render(state);
    return state;
  } catch (error) {
    note(error.message, true);
    return null;
  }
}

function startPolling() {
  if (STATE.polling) return;
  STATE.polling = setInterval(async () => {
    const job = await api('/api/job').catch(() => null);
    if (!job) return;
    renderJob(job);
    if (job.state !== 'running') {
      clearInterval(STATE.polling);
      STATE.polling = null;
      setControlsDisabled(false);
      note(job.state === 'failed' ? job.error : '');
      await refresh();
    }
  }, 400);
}

function setControlsDisabled(disabled) {
  for (const id of ['seedBtn', 'verifyBtn', 'resetBtn', 'ccpSwitch', 'projBtn']) $(id).disabled = disabled;
}

async function toggleCcp() {
  const enabled = $('ccpSwitch').getAttribute('aria-checked') !== 'true';
  if (!STATE.state || !STATE.state.seeded) {
    note(t('seedFirst'), true);
    return;
  }
  setControlsDisabled(true);
  $('switchState').textContent = t('switchWorking');
  try {
    const result = await api('/api/mode', { method: 'POST', body: JSON.stringify({ enabled }) });
    render(result.state);
    const s = result.switch;
    const box = $('switchResult');
    box.classList.remove('hidden');
    box.textContent = t('migrated')(
      s.migrated,
      fmtBytes(s.disk_bytes_before),
      fmtBytes(s.disk_bytes_after),
      s.seconds,
    );
    note('');
  } catch (error) {
    note(error.message, true);
    await refresh();
  } finally {
    setControlsDisabled(false);
  }
}

async function seed() {
  setControlsDisabled(true);
  note(t('seeding'));
  try {
    const job = await api('/api/seed', { method: 'POST', body: JSON.stringify({ reset: true }) });
    renderJob(job);
    startPolling();
  } catch (error) {
    note(error.message, true);
    setControlsDisabled(false);
  }
}

async function verify() {
  if (!STATE.state || !STATE.state.seeded) {
    note(t('seedFirst'), true);
    return;
  }
  setControlsDisabled(true);
  const box = $('verifyBox');
  clear(box);
  box.appendChild(el('p', 'muted', '…'));
  try {
    const report = await api('/api/verify', { method: 'POST', body: JSON.stringify({}) });
    clear(box);

    const summary = el('div', 'verify-summary');
    summary.textContent = report.all_lossless
      ? t('verifiedAll')(report.variants_checked, report.total_seconds)
      : t('verifyFailed');
    box.appendChild(summary);

    const chain = el('p', 'muted', report.ledger_chain_ok ? t('chainOk') : t('chainBad'));
    box.appendChild(chain);

    for (const row of report.results) {
      const line = el('div', 'verify-row');
      line.appendChild(el('span', null, row.label || row.variant_id));
      const right = el('span', row.lossless ? 'ok' : 'bad');
      right.textContent = row.lossless
        ? `${t('lossless')} · ${fmtBytes(row.bytes)} in ${row.seconds}s${row.throughput_mib_s ? ` · ${row.throughput_mib_s} MiB/s` : ''}`
        : `${t('notLossless')}${row.error ? ` · ${row.error}` : ''}`;
      line.appendChild(right);
      box.appendChild(line);
    }

    const badge = $('losslessBadge');
    badge.textContent = report.all_lossless ? t('lossless') : t('notLossless');
    badge.className = `badge ${report.all_lossless ? 'ok' : 'bad'}`;
    $('losslessSub').textContent = `${report.variants_checked} variants · ${report.total_seconds}s · mode ${report.mode}`;
  } catch (error) {
    clear(box);
    box.appendChild(el('p', 'muted', error.message));
  } finally {
    setControlsDisabled(false);
  }
}

async function reset() {
  setControlsDisabled(true);
  try {
    const state = await api('/api/reset', { method: 'POST', body: JSON.stringify({}) });
    render(state);
    clear($('verifyBox'));
    $('verifyBox').appendChild(el('p', 'muted', t('verifyEmpty')));
    $('losslessBadge').textContent = t('notChecked');
    $('losslessBadge').className = 'badge neutral';
    $('losslessSub').textContent = '—';
    $('switchResult').classList.add('hidden');
    clear($('planeBox'));
    $('planeBox').appendChild(el('p', 'muted', t('planeEmpty')));
    STATE.lastInspected = null;
  } catch (error) {
    note(error.message, true);
  } finally {
    setControlsDisabled(false);
  }
}

async function recalculate() {
  try {
    const projection = await api('/api/projection', {
      method: 'POST',
      body: JSON.stringify({
        params_billions: Number($('projParams').value),
        variants: Number($('projVariants').value),
        downloads_per_variant: Number($('projDownloads').value),
      }),
    });
    renderProjection(projection);
    note('');
  } catch (error) {
    note(error.message, true);
  }
}

async function inspect(variantId) {
  try {
    const detail = await api(`/api/variants/${encodeURIComponent(variantId)}/inspect`);
    STATE.lastInspected = detail;
    renderPlanes(detail);
    openDrawer(detail);
  } catch (error) {
    note(error.message, true);
  }
}

function openDrawer(detail) {
  $('drawerTitle').textContent = detail.label || detail.variant.id;
  const body = $('drawerBody');
  clear(body);

  if (detail.note) body.appendChild(el('p', 'drawer-note', detail.note));

  body.appendChild(el('h3', null, t('composition')));
  const opTable = el('table');
  const opBody = el('tbody');
  const labels = { copy_base: t('copied'), delta: t('deltaed'), literal: t('whole') };
  for (const [op, bucket] of Object.entries(detail.by_op || {})) {
    const row = el('tr');
    row.appendChild(el('td', null, labels[op] || op));
    row.appendChild(el('td', 'num', `${bucket.count}×`));
    row.appendChild(el('td', 'num', fmtBytes(bucket.raw_bytes)));
    row.appendChild(el('td', 'num', fmtBytes(bucket.stored_bytes)));
    opBody.appendChild(row);
  }
  opTable.appendChild(opBody);
  body.appendChild(opTable);

  body.appendChild(el('h3', null, t('perTensor')));
  const table = el('table');
  const head = el('thead');
  const headRow = el('tr');
  for (const [key, cls] of [['thTensor', ''], ['thShape', ''], ['thOp', ''], ['thStored', 'num'], ['thRatio', 'num']]) {
    headRow.appendChild(el('th', cls, t(key)));
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const tbody = el('tbody');
  for (const tensor of (detail.tensors || []).slice(0, 60)) {
    const row = el('tr');
    row.appendChild(el('td', null, tensor.name));
    row.appendChild(el('td', null, Array.isArray(tensor.shape) ? tensor.shape.join('×') : '—'));
    const opCell = el('td');
    opCell.appendChild(el('span', `op-tag op-${tensor.op}`, tensor.op));
    row.appendChild(opCell);
    row.appendChild(el('td', 'num', fmtBytes(tensor.stored_bytes)));
    row.appendChild(el('td', 'num', tensor.ratio === null ? '0%' : fmtPct(tensor.ratio)));
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  body.appendChild(table);

  $('drawer').classList.remove('hidden');
}

async function loadLedger() {
  try {
    const data = await api('/api/ledger?limit=25');
    const box = $('ledgerBox');
    clear(box);
    if (!data.entries.length) {
      box.appendChild(el('p', 'muted', '—'));
      return;
    }
    const chain = el('p', 'muted', data.chain_ok ? t('chainOk') : `${t('chainBad')} · ${data.chain_error || ''}`);
    box.appendChild(chain);
    for (const entry of [...data.entries].reverse()) {
      const row = el('div', 'ledger-entry');
      row.appendChild(el('span', 'ledger-seq', `#${entry.seq}`));
      const right = el('div');
      right.appendChild(el('div', 'ledger-action', entry.action));
      right.appendChild(el('div', 'ledger-detail', JSON.stringify(entry.detail)));
      row.appendChild(right);
      box.appendChild(row);
    }
  } catch (error) {
    /* the ledger panel is decorative for the pitch; a failure must not take the
       page down with it. The error still surfaces in the action note. */
    note(error.message, true);
  }
}

/* ── wiring ────────────────────────────────────────────────────────────── */

$('ccpSwitch').addEventListener('click', toggleCcp);
$('seedBtn').addEventListener('click', seed);
$('verifyBtn').addEventListener('click', verify);
$('resetBtn').addEventListener('click', reset);
$('projBtn').addEventListener('click', recalculate);
$('drawerClose').addEventListener('click', () => $('drawer').classList.add('hidden'));
$('langToggle').addEventListener('click', () => {
  STATE.lang = STATE.lang === 'en' ? 'he' : 'en';
  applyLanguage();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') $('drawer').classList.add('hidden');
});

applyLanguage();
refresh().then((state) => {
  if (state && state.job && state.job.state === 'running') {
    setControlsDisabled(true);
    startPolling();
  }
});
