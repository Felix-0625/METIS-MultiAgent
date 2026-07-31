/* ═══════════════════════════════════════════════════════════════
   MeTis — Promo Effects (performance optimized)
   TargetCursor · BorderGlow · FlowingMenu · TextPressure · PillNav
   ═══════════════════════════════════════════════════════════════ */
(function(){'use strict';
var initDone = false;
function init(){
  if(typeof gsap==='undefined'){setTimeout(init,100);return;}
  if(initDone) return; initDone=true;
  initTargetCursor();
  initBorderGlow();
  initFlowingMenu();
  initTextPressure();
  initPillNav();
}

/* ── TargetCursor ──────────────────────────────────────── */
function initTargetCursor(){
  var spinDuration = 2, hideDefaultCursor = true, hoverDuration = 0.2;
  var cursorColor = '#ffffff', cursorColorOnTarget = '#B497CF';
  var targetSelector = '.cursor-target', borderWidth = 3, cornerSize = 12;

  var w = document.createElement('div');
  w.className = 'target-cursor-wrapper';
  w.innerHTML =
    '<div class="target-cursor-dot" style="background-color:'+cursorColor+'"></div>'+
    '<div class="target-cursor-corner corner-tl" style="border-color:'+cursorColor+'"></div>'+
    '<div class="target-cursor-corner corner-tr" style="border-color:'+cursorColor+'"></div>'+
    '<div class="target-cursor-corner corner-br" style="border-color:'+cursorColor+'"></div>'+
    '<div class="target-cursor-corner corner-bl" style="border-color:'+cursorColor+'"></div>';
  document.body.appendChild(w);

  var cursor = w, dot = w.querySelector('.target-cursor-dot');
  var cornersArr = Array.from(w.querySelectorAll('.target-cursor-corner'));

  if (hideDefaultCursor) { document.body.style.cursor = 'none'; document.documentElement.style.cursor = 'none'; }

  gsap.set(cursor, { xPercent: -50, yPercent: -50, x: window.innerWidth/2, y: window.innerHeight/2 });
  var spinTl = gsap.timeline({ repeat: -1 }).to(cursor, { rotation: '+=360', duration: spinDuration, ease: 'none' });

  var activeTarget = null, currentLeaveHandler = null, resumeTimeout = null;
  var targetCornerPositions = null, activeStrength = { current: 0 };
  var tickerFn = null, rafId = null;
  var lastMoveX = window.innerWidth/2, lastMoveY = window.innerHeight/2;

  /* ── Throttled move via RAF ───────────────────── */
  var pendingMove = null;
  function scheduleMove(x, y) {
    pendingMove = { x: x, y: y };
    if (!rafId) rafId = requestAnimationFrame(applyMove);
  }
  function applyMove() {
    rafId = null;
    if (!pendingMove) return;
    var m = pendingMove; pendingMove = null;
    lastMoveX = m.x; lastMoveY = m.y;
    gsap.to(cursor, { x: m.x, y: m.y, duration: 0.08, ease: 'power2.out' });
  }
  window.addEventListener('mousemove', function(e) { scheduleMove(e.clientX, e.clientY); });

  /* ── Mouse down/up ────────────────────────────── */
  window.addEventListener('mousedown', function() { gsap.to(dot, { scale: 0.7, duration: 0.25 }); });
  window.addEventListener('mouseup', function() { gsap.to(dot, { scale: 1, duration: 0.25 }); });

  /* ── Ticker: lerp corners on hover ───────────── */
  tickerFn = function() {
    if (!targetCornerPositions) return;
    var str = activeStrength.current; if (str<0.01) return;
    var cx = gsap.getProperty(cursor, 'x'), cy = gsap.getProperty(cursor, 'y');
    cornersArr.forEach(function(corner, i) {
      var curX = gsap.getProperty(corner, 'x'), curY = gsap.getProperty(corner, 'y');
      var tx = targetCornerPositions[i].x - cx, ty = targetCornerPositions[i].y - cy;
      gsap.to(corner, { x: curX+(tx-curX)*str, y: curY+(ty-curY)*str, duration: 0.05, ease: 'none', overwrite:'auto' });
    });
  };

  function cleanupTarget(target) {
    if (currentLeaveHandler) { target.removeEventListener('mouseleave', currentLeaveHandler); currentLeaveHandler = null; }
  }

  /* ── Enter handler ───────────────────────────── */
  var enterHandler = function(e) {
    var el = e.target, target = null;
    while (el && el !== document.body) { if (el.matches && el.matches(targetSelector)) { target = el; break; } el = el.parentElement; }
    if (!target || target === activeTarget) return;
    if (activeTarget) { cleanupTarget(activeTarget); }
    if (resumeTimeout) { clearTimeout(resumeTimeout); resumeTimeout = null; }
    activeTarget = target;

    cornersArr.forEach(function(c) { gsap.killTweensOf(c, 'x,y'); });
    gsap.killTweensOf(cursor, 'rotation'); spinTl.pause(); gsap.set(cursor, { rotation: 0 });

    if (cursorColorOnTarget) {
      gsap.to(cornersArr, { borderColor: cursorColorOnTarget, duration: 0.12, ease: 'power2.out' });
      gsap.to(dot, { backgroundColor: cursorColorOnTarget, duration: 0.12, ease: 'power2.out' });
    }

    var rect = target.getBoundingClientRect();
    targetCornerPositions = [
      { x: rect.left - borderWidth, y: rect.top - borderWidth },
      { x: rect.right + borderWidth - cornerSize, y: rect.top - borderWidth },
      { x: rect.right + borderWidth - cornerSize, y: rect.bottom + borderWidth - cornerSize },
      { x: rect.left - borderWidth, y: rect.bottom + borderWidth - cornerSize }
    ];

    gsap.ticker.add(tickerFn);
    gsap.to(activeStrength, { current: 1, duration: hoverDuration, ease: 'power2.out' });

    var cx = gsap.getProperty(cursor, 'x'), cy = gsap.getProperty(cursor, 'y');
    cornersArr.forEach(function(corner, i) {
      gsap.to(corner, { x: targetCornerPositions[i].x - cx, y: targetCornerPositions[i].y - cy, duration: 0.15, ease: 'power2.out' });
    });

    var leaveHandler = function() { gsap.ticker.remove(tickerFn); targetCornerPositions = null; gsap.set(activeStrength, { current: 0 }); activeTarget = null;
      if (cursorColorOnTarget) { gsap.to(cornersArr, { borderColor: cursorColor, duration: 0.12, ease: 'power2.out' }); gsap.to(dot, { backgroundColor: cursorColor, duration: 0.12, ease: 'power2.out' }); }
      gsap.killTweensOf(cornersArr, 'x,y');
      var pos = [{ x: -cornerSize*1.5, y: -cornerSize*1.5 }, { x: cornerSize*0.5, y: -cornerSize*1.5 }, { x: cornerSize*0.5, y: cornerSize*0.5 }, { x: -cornerSize*1.5, y: cornerSize*0.5 }];
      var tl = gsap.timeline(); cornersArr.forEach(function(corner, idx) { tl.to(corner, { x: pos[idx].x, y: pos[idx].y, duration: 0.25, ease: 'power3.out' }, 0); });
      resumeTimeout = setTimeout(function() { if (!activeTarget) { var cr = gsap.getProperty(cursor, 'rotation') % 360; spinTl.kill(); spinTl = gsap.timeline({ repeat: -1 }).to(cursor, { rotation: '+=360', duration: spinDuration, ease: 'none' }); } resumeTimeout = null; }, 50);
      cleanupTarget(target);
    };
    currentLeaveHandler = leaveHandler; target.addEventListener('mouseleave', leaveHandler);
  };
  window.addEventListener('mouseover', enterHandler, { passive: true });

  window.addEventListener('scroll', function() {
    if (!activeTarget) return;
    var cx = gsap.getProperty(cursor, 'x'), cy = gsap.getProperty(cursor, 'y');
    var elUnder = document.elementFromPoint(cx, cy);
    if (!(elUnder && (elUnder === activeTarget || elUnder.closest(targetSelector) === activeTarget)) && currentLeaveHandler) currentLeaveHandler();
  }, { passive: true });
}

/* ── BorderGlow ───────────────────────────────────────────── */
function initBorderGlow(){
  document.querySelectorAll('.border-glow-card').forEach(function(card){
    if(!card.querySelector('.edge-light')){ var edge=document.createElement('span'); edge.className='edge-light'; card.appendChild(edge); }
    card.addEventListener('mousemove',function(e){
      var r=card.getBoundingClientRect();
      var cx=r.left+r.width/2,cy=r.top+r.height/2;
      var dx=e.clientX-cx,dy=e.clientY-cy;
      var dist=Math.sqrt(dx*dx+dy*dy);
      var maxDist=Math.sqrt(r.width*r.width+r.height*r.height)/2;
      card.style.setProperty('--ep',Math.max(0,Math.min(100,(1-dist/maxDist)*100)));
      card.style.setProperty('--ca',(Math.atan2(dy,dx)*180/Math.PI+180)+'deg');
    });
    card.addEventListener('mouseleave',function(){ card.style.setProperty('--ep','0'); });
  });
}

/* ── FlowingMenu ──────────────────────────────────────────── */
function initFlowingMenu(){
  document.querySelectorAll('.menu__item').forEach(function(item){
    var marquee=item.querySelector('.marquee'), inner=marquee ? marquee.querySelector('.marquee__inner') : null;
    if(!marquee||!inner) return;
    var tl=gsap.timeline({paused:true});
    tl.to(marquee,{y:'0%',duration:0.3,ease:'power2.out'},0);
    tl.to(inner,{xPercent:-50,duration:8,ease:'none',repeat:-1},0);
    item.addEventListener('mouseenter',function(){tl.play();});
    item.addEventListener('mouseleave',function(){tl.reverse();});
  });
}

/* ── TextPressure (optimized: cached positions, batched updates) ── */
function initTextPressure(){
  var container = document.querySelector('.text-pressure-wrap');
  if(!container) return;
  var text = container.getAttribute('data-text') || '';
  container.style.cssText = 'position:relative;width:100%;background:transparent;overflow:hidden';

  var h1 = document.createElement('h1');
  h1.className = 'text-pressure-title';
  h1.style.cssText = 'font-family:Inter,sans-serif;font-weight:100;margin:0;text-align:center;user-select:none;width:100%;transform-origin:center top;line-height:1.3';

  var chars = text.split(''), spans = [];
  chars.forEach(function(ch) {
    var span = document.createElement('span');
    span.setAttribute('data-char', ch);
    span.textContent = ch;
    span.style.cssText = 'display:inline-block;will-change:transform';
    h1.appendChild(span);
    spans.push(span);
  });
  container.appendChild(h1);

  /* Cache char center positions to avoid per-frame getBoundingClientRect */
  var charCenters = [];
  var containerRect = container.getBoundingClientRect();
  var titleWidth = 0;

  function updateCharCenters() {
    var h1Rect = h1.getBoundingClientRect();
    titleWidth = h1Rect.width;
    charCenters = [];
    for(var i=0;i<spans.length;i++) {
      var r = spans[i].getBoundingClientRect();
      charCenters.push({ x: r.x + r.width/2, y: r.y + r.height/2 });
    }
  }
  updateCharCenters();

  var recalcTimer;
  function scheduleRecalc() { clearTimeout(recalcTimer); recalcTimer = setTimeout(updateCharCenters, 300); }
  window.addEventListener('scroll', scheduleRecalc, { passive: true });
  window.addEventListener('resize', function() { updateCharCenters(); scheduleRecalc(); });

  var mouseRef = { x: 0, y: 0 }, cursorRef = { x: 0, y: 0 };
  var initRect = container.getBoundingClientRect();
  mouseRef.x = initRect.left + initRect.width/2; mouseRef.y = initRect.top + initRect.height/2;
  cursorRef.x = mouseRef.x; cursorRef.y = mouseRef.y;

  window.addEventListener('mousemove', function(e) { cursorRef.x = e.clientX; cursorRef.y = e.clientY; });

  function getAttr(distance, maxDist, minVal, maxVal) {
    var val = maxVal - Math.abs((maxVal * distance) / maxDist);
    return Math.max(minVal, val + minVal);
  }

  /* Auto-size font */
  function setSize() {
    var cw = container.getBoundingClientRect().width;
    var s = Math.min(Math.max(cw / (chars.length / 1.5), 20), 56);
    h1.style.fontSize = s + 'px';
    updateCharCenters();
  }
  setSize();
  var resizeTimer; window.addEventListener('resize', function() { clearTimeout(resizeTimer); resizeTimer = setTimeout(setSize, 100); });

  /* Animation loop — batched writes to minimize layout thrashing */
  var lastWght = new Int8Array(spans.length);
  lastWght.fill(-1);
  var lastWdth = new Int8Array(spans.length);
  lastWdth.fill(-1);
  var lastItal = new Int8Array(spans.length);
  lastItal.fill(-1);

  function animate() {
    mouseRef.x += (cursorRef.x - mouseRef.x) / 12;
    mouseRef.y += (cursorRef.y - mouseRef.y) / 12;

    var maxDist = titleWidth * 0.65;
    var mx = mouseRef.x, my = mouseRef.y;

    for(var i=0;i<spans.length;i++) {
      var cc = charCenters[i];
      if(!cc) continue;
      var dx = mx - cc.x, dy = my - cc.y;
      var d = Math.sqrt(dx*dx + dy*dy);

      var wght = Math.floor(getAttr(d, maxDist, 100, 900));
      var wdth = Math.floor(getAttr(d, maxDist, 50, 200));
      var ital = (getAttr(d, maxDist, 0, 1) * 15) | 0;

      /* Only write if changed */
      if(lastWght[i] !== wght) { spans[i].style.fontWeight = wght; lastWght[i] = wght; }
      if(lastWdth[i] !== wdth) { spans[i].style.letterSpacing = (((200-wdth)/150)*2.5).toFixed(2)+'px'; lastWdth[i] = wdth; }
      if(lastItal[i] !== ital) { spans[i].style.transform = 'skewX(-'+ital+'deg)'; lastItal[i] = ital; }
    }
    requestAnimationFrame(animate);
  }
  animate();
}

/* ── PillNav (delegated to main.js to avoid duplicate bindings) ── */
function initPillNav(){
  /* Pill nav click handling is in main.js (scrollToSection + updateUI) */
}

if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();
})();