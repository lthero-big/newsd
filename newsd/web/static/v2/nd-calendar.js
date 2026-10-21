// Shared week⇄month calendar + layered art date. Used by home.html and favorites.html.
// DDCal({ calbox, days: () => ({ 'YYYY-MM-DD': count }), sel, onSelect(sel, prev), disableEmpty, onAll, allLabel, allSub, subOf(d), unit, dotLabel, countLabel(c), emptyLabel })
// Art: outline + gradient-fill layers (21st "layered text"); digit roll spring = cult-ui rolling-number (mass .8, k 75, c 15). Reduced motion / 减少动效 → crossfade/instant.
window.DDCal = function (o) {
  const calbox = o.calbox, cal = calbox.querySelector('.cal'), roll = calbox.querySelector('.roll');
  const RM = () => matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('dd-rm');
  const pad = n => String(n).padStart(2, '0'), ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const WK = ['一', '二', '三', '四', '五', '六', '日'];
  let sel = o.sel || null, focusDay = sel, view = (() => { const ks = Object.keys(o.days()).sort(); const s = sel || ks[ks.length - 1] || ymd(new Date()); const [y, m] = s.split('-').map(Number); return new Date(y, m - 1, 1); })();
  // ---------- art date: layered outline + gradient-fill digits (21st "layered text"), spring roll per digit (cult-ui rolling-number: mass .8, k 75, c 15) ----------
  const MON = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  function setRoll(num, sub) { const art = roll; const oldCols = [...art.querySelectorAll('.rs')].map(c => +(c.dataset.v || 0)); const ds = [...String(num)];
    const layer = cls => `<span class="rl ${cls}" aria-hidden="true">${ds.map(ch => /\d/.test(ch) ? `<span class="rc"><span class="rs">${[...'0123456789'].map(d => `<i>${d}</i>`).join('')}</span></span>` : `<span class="rx">${ch}</span>`).join('')}</span>`;
    art.innerHTML = layer('o') + layer('f') + `<span class="rm" aria-hidden="true">${sub}</span>`; art.setAttribute('aria-label', sub + ' ' + num);
    const cols = [...art.querySelectorAll('.rs')], n = ds.filter(ch => /\d/.test(ch)).length, digits = ds.filter(ch => /\d/.test(ch)).map(Number);
    const to = i => digits[i % n];
    if (RM()) { cols.forEach((c, i) => { c.style.transform = `translateY(${-to(i)}em)`; c.dataset.v = to(i); }); art.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220 }); return; }
    const st = cols.map((c, i) => ({ x: oldCols[i] ?? 0, v: 0, to: to(i), d: Math.floor(i / n) * 60 + (i % n) * 70 })); const t0 = performance.now(); let last = t0;
    const tick = now => { const dt = Math.min(.032, (now - last) / 1000); last = now; let moving = false;
      st.forEach((p, i) => { if (now - t0 >= p.d) for (let s = 0; s < 4; s++) { const acc = (-75 * (p.x - p.to) - 15 * p.v) / .8; p.v += acc * dt / 4; p.x += p.v * dt / 4; }
        cols[i].style.transform = `translateY(${-p.x}em)`; cols[i].dataset.v = p.to; if (now - t0 < p.d || Math.abs(p.x - p.to) > .002 || Math.abs(p.v) > .01) moving = true; });
      if (moving) requestAnimationFrame(tick); else st.forEach((p, i) => cols[i].style.transform = `translateY(${-p.to}em)`); };
    requestAnimationFrame(tick); }
  const showRoll = () => { if (sel) { const [, m, d] = sel.split('-').map(Number); setRoll(d, `${MON[m - 1]} · ${sel.slice(0, 4)}${o.subOf ? o.subOf(sel) : ''}`); } else setRoll(Object.values(o.days()).reduce((a, n) => a + n, 0), o.allSub || 'ALL'); };
  // ---------- calendar: week strip ⇄ month (clip window morph: height + translateY of the week row) ----------
  let open = false; const mobile = () => matchMedia('(max-width:560px)').matches;
  function drawCal(slide = 0) { const fd = o.days(), y = view.getFullYear(), mo = view.getMonth(), first = new Date(y, mo, 1), off = (first.getDay() + 6) % 7, n = new Date(y, mo + 1, 0).getDate(), today = ymd(new Date());
    const days = Object.keys(fd).sort(), prevD = sel ? days.filter(d => d < sel).pop() : days[days.length - 1], nextD = sel ? days.find(d => d > sel) : null;
    const inM = k => k && k.startsWith(`${y}-${pad(mo + 1)}`);
    if (!inM(focusDay)) focusDay = inM(sel) ? sel : inM(today) ? today : `${y}-${pad(mo + 1)}-01`;
    const rows = Math.ceil((off + n) / 7), start = new Date(y, mo, 1 - off); let cells = '', row = 0;
    for (let i = 0; i < rows * 7; i++) { const dt = new Date(start); dt.setDate(start.getDate() + i); const k = ymd(dt), c = fd[k] || 0, out = dt.getMonth() !== mo; if (k === focusDay) row = Math.floor(i / 7);
      cells += `<button type="button" class="cd${c ? ' has' : ''}${out ? ' out' : ''}${k === sel ? ' sel' : ''}${k === today ? ' today' : ''}"${!c && o.disableEmpty ? ' aria-disabled="true"' : ''} data-d="${k}" tabindex="${k === focusDay ? 0 : -1}" aria-pressed="${k === sel}" aria-label="${dt.getMonth() + 1}月${dt.getDate()}日${c ? (o.countLabel ? o.countLabel(c) : `，${c} 个收藏`) : (o.emptyLabel || '，无收藏')}"><span>${dt.getDate()}</span>${c ? `<i class="dots">${'<b></b>'.repeat(Math.min(c, 3))}</i>` : ''}</button>`; }
    const prevH = cal.querySelector('.cgw')?.offsetHeight;
    cal.innerHTML = `<div class="ch"><button type="button" class="cm" data-m="-1" aria-label="${open ? '上个月' : '上一周'}">‹</button><button type="button" class="ct" aria-expanded="${open}" aria-label="${open ? '收起为周视图' : '展开整月'}">${y} 年 ${mo + 1} 月 <span class="cv">${open ? '收起' : '整月'}<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></span></button><button type="button" class="cm" data-m="1" aria-label="${open ? '下个月' : '下一周'}">›</button></div>
<div class="cw" aria-hidden="true">${WK.map(w => `<span>${w}</span>`).join('')}</div><div class="cgw"><div class="cg" role="group" aria-label="${y} 年 ${mo + 1} 月，带点的日子有${o.dotLabel || '收藏'}">${cells}</div></div>
<div class="cq"><button type="button" class="cj" data-j="${prevD || ''}" ${prevD ? '' : 'disabled'} aria-label="上一个${o.unit}">‹ ${prevD ? prevD.slice(5).replace('-', '/') : '—'}</button><button type="button" class="call${sel || o.onAll ? '' : ' on'}" data-all aria-pressed="${!sel && !o.onAll}">${o.allLabel || '全部'}</button><button type="button" class="cj" data-j="${nextD || ''}" ${nextD ? '' : 'disabled'} aria-label="下一个${o.unit}">${nextD ? nextD.slice(5).replace('-', '/') : '—'} ›</button></div>`;
    cal.classList.toggle('open', open); calbox.classList.toggle('open', open);
    const w = cal.querySelector('.cgw'), g = cal.querySelector('.cg'), rh = g.firstElementChild.offsetHeight + 2;
    const H = open ? rows * rh - 2 : rh - 2, TY = open ? 0 : -row * rh;
    if (prevH != null && prevH > 0 && !RM() && !slide) { w.style.height = prevH + 'px'; g.style.transform = `translateY(${cal._ty || 0}px)`; w.offsetHeight; w.style.transition = g.style.transition = 'height .48s cubic-bezier(.32,1.2,.5,1),transform .48s cubic-bezier(.32,1.2,.5,1)'; }
    const base = cal.offsetHeight - w.offsetHeight; calbox.style.setProperty('--wk', (base + rh - 2) + 'px');
    w.style.height = H + 'px'; g.style.transform = `translateY(${TY}px)`; cal._ty = TY;
    if (slide && !RM()) g.animate([{ opacity: 0, translate: `${slide * 28}px 0`, filter: 'blur(3px)' }, { opacity: 1, translate: '0 0', filter: 'blur(0)' }], { duration: 380, easing: 'cubic-bezier(.22,1,.36,1)' }); }
  
  function setView(d, slide) { const nv = new Date(d.getFullYear(), d.getMonth(), 1); const s = slide ?? (nv.getTime() === view.getTime() ? 0 : (nv > view ? 1 : -1)); view = nv; focusDay = ymd(d); drawCal(s); }
  function toggle(v = !open) { if (v === open) return; open = v; drawCal(); if (open) cal.querySelector('.cd[tabindex="0"]')?.focus({ preventScroll: true }); }
  function choose(d, opts = {}) { if (d && o.disableEmpty && !o.days()[d]) return; if (!d && o.onAll && !opts.silent) return o.onAll(); const prev = sel; sel = d || null; if (sel) { if (mobile()) open = false; setView(new Date(sel + 'T00:00')); } else drawCal();
    showRoll(); o.onSelect && o.onSelect(sel, prev);
    if (opts.focus) { const b = cal.querySelector(`[data-d="${focusDay}"]`); b && b.focus({ preventScroll: true }); } }
  cal.addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return;
    if (b.classList.contains('ct')) return toggle();
    if (b.dataset.m) { const d = new Date(focusDay + 'T00:00'); if (open) { d.setDate(1); d.setMonth(d.getMonth() + +b.dataset.m); setView(d, +b.dataset.m); } else { d.setDate(d.getDate() + 7 * b.dataset.m); setView(d, 0); } return; }
    if ('all' in b.dataset) return o.onAll ? o.onAll() : choose(null); if (b.dataset.j) return choose(b.dataset.j); if (b.dataset.d) choose(b.dataset.d === sel && !o.onAll ? null : b.dataset.d, { focus: !mobile() }); });
  cal.addEventListener('keydown', e => { if (e.key === 'Escape' && open) { toggle(false); cal.querySelector('.ct').focus(); return; }
    const b = e.target.closest('.cd[data-d]'); if (!b) return; const d = new Date(b.dataset.d + 'T00:00');
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); return choose(b.dataset.d === sel && !o.onAll ? null : b.dataset.d, { focus: true }); }
    const mv = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (mv || e.key === 'PageUp' || e.key === 'PageDown' || e.key === 'Home' || e.key === 'End') { e.preventDefault();
      if (mv) d.setDate(d.getDate() + mv); else if (e.key === 'PageUp') d.setMonth(d.getMonth() - 1); else if (e.key === 'PageDown') d.setMonth(d.getMonth() + 1);
      else if (e.key === 'Home') d.setDate(d.getDate() - (d.getDay() + 6) % 7); else d.setDate(d.getDate() + 6 - (d.getDay() + 6) % 7);
      setView(d); cal.querySelector(`[data-d="${focusDay}"]`).focus({ preventScroll: true }); } });
  document.addEventListener('pointerdown', e => { if (open && mobile() && !cal.contains(e.target)) toggle(false); });
  showRoll(); drawCal();
  return { choose, redraw: () => drawCal(), roll: showRoll, get sel() { return sel; } };
};
