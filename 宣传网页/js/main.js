/* ═══════════════════════════════════════════════════════════════
   MeTis — Navigation & Interactions
   ═══════════════════════════════════════════════════════════════ */
(function() {
  'use strict';

  var container = document.getElementById('scrollContainer');
  var sections = document.querySelectorAll('.section');
  var pills = document.querySelectorAll('.pill[data-nav]');
  var pageDots = document.querySelectorAll('.page-dot');
  var navbar = document.getElementById('navbar');
  var loginOverlay = document.getElementById('loginOverlay');
  var FRONTEND_URL = '/app/';
  var API_BASE_URL = '/api';

  var currentSection = 0;
  var isScrolling = false;
  var scrollTimeout = null;

  /* ── Scroll to a section by index ─────────────────────────── */
  window.scrollToSection = function(index) {
    if (isScrolling) return;
    if (index < 0 || index >= sections.length) return;
    var target = sections[index];
    if (target) {
      isScrolling = true;
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      currentSection = index;
      updateUI(index);
      setTimeout(function() { isScrolling = false; }, 600);
    }
  };

  /* ── Update pills, dots, and section visibility ──────────── */
  function updateUI(index) {
    pills.forEach(function(pill, i) {
      var navIdx = parseInt(pill.getAttribute('data-nav'), 10);
      pill.classList.toggle('active', navIdx === index);
    });
    pageDots.forEach(function(dot, i) {
      var dotIdx = parseInt(dot.getAttribute('data-index'), 10);
      dot.classList.toggle('active', dotIdx === index);
    });
    sections.forEach(function(section, i) {
      var secIdx = parseInt(section.getAttribute('data-index'), 10);
      section.classList.toggle('visible', secIdx === index);
    });
    if (navbar) {
      if (index > 0) {
        navbar.classList.add('scrolled');
      } else {
        navbar.classList.remove('scrolled');
      }
    }
  }

  /* ── Find closest section to scroll position ─────────────── */
  function getActiveSection() {
    if (!container) return 0;
    var scrollTop = container.scrollTop;
    var viewHeight = container.clientHeight;
    var bestIndex = 0;
    var bestAmount = 0;
    for (var i = 0; i < sections.length; i++) {
      var section = sections[i];
      var offsetTop = section.offsetTop;
      var offsetBottom = offsetTop + section.offsetHeight;
      var visibleStart = Math.max(scrollTop, offsetTop);
      var visibleEnd = Math.min(scrollTop + viewHeight, offsetBottom);
      var visibleAmount = Math.max(0, visibleEnd - visibleStart);
      if (visibleAmount > bestAmount) {
        bestAmount = visibleAmount;
        bestIndex = i;
      }
    }
    return bestIndex;
  }

  /* ── Scroll event handler ─────────────────────────────────── */
  if (container) {
    container.addEventListener('scroll', function() {
      if (scrollTimeout) clearTimeout(scrollTimeout);
      scrollTimeout = setTimeout(function() {
        var active = getActiveSection();
        if (active !== currentSection) {
          currentSection = active;
          updateUI(active);
        }
      }, 80);
    });
  }

  /* ── Pill nav clicks ──────────────────────────────────────── */
  pills.forEach(function(pill) {
    pill.addEventListener('click', function(e) {
      e.preventDefault();
      var index = parseInt(this.getAttribute('data-nav'), 10);
      if (!isNaN(index)) scrollToSection(index);
    });
  });

  /* ── Page dot clicks ──────────────────────────────────────── */
  pageDots.forEach(function(dot) {
    dot.addEventListener('click', function() {
      var index = parseInt(this.getAttribute('data-index'), 10);
      if (!isNaN(index)) scrollToSection(index);
    });
  });

  /* ── Pill logo click — scroll to top ─────────────────────── */
  var pillLogo = document.querySelector('.pill-logo');
  if (pillLogo) {
    pillLogo.addEventListener('click', function(e) {
      e.preventDefault();
      scrollToSection(0);
    });
  }

  /* ── Keyboard navigation ─────────────────────────────────── */
  document.addEventListener('keydown', function(e) {
    if (loginOverlay && loginOverlay.classList.contains('visible')) return;
    var tagName = e.target && e.target.tagName;
    if (tagName === 'INPUT' || tagName === 'TEXTAREA') return;

    if ((e.key === 'ArrowDown' || e.key === 'PageDown') && currentSection < sections.length - 1) {
      e.preventDefault();
      scrollToSection(currentSection + 1);
    } else if ((e.key === 'ArrowUp' || e.key === 'PageUp') && currentSection > 0) {
      e.preventDefault();
      scrollToSection(currentSection - 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      scrollToSection(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      scrollToSection(sections.length - 1);
    }
  });

  /* ── Touch scroll handling ────────────────────────────────── */
  var touchStartY = 0;
  var touchEndY = 0;

  if (container) {
    container.addEventListener('touchstart', function(e) {
      touchStartY = e.changedTouches[0].screenY;
    }, { passive: true });

    container.addEventListener('touchend', function(e) {
      touchEndY = e.changedTouches[0].screenY;
      var diff = touchStartY - touchEndY;
      if (Math.abs(diff) > 50) {
        var dir = diff > 0 ? 1 : -1;
        var target = currentSection + dir;
        if (target >= 0 && target < sections.length) {
          scrollToSection(target);
        }
      }
    }, { passive: true });
  }

  /* ── Wheel: prevent multi-section skips ───────────────────── */
  var wheelTimeout = null;
  var wheelAccum = 0;
  if (container) {
    container.addEventListener('wheel', function(e) {
      wheelAccum += Math.abs(e.deltaY);
      if (wheelTimeout) {
        if (wheelAccum < 200) {
          e.preventDefault();
        }
        return;
      }
      var dir = e.deltaY > 0 ? 1 : -1;
      var target = currentSection + dir;
      if (target >= 0 && target < sections.length && target !== currentSection && wheelAccum < 200) {
        scrollToSection(target);
      }
      wheelAccum = 0;
      wheelTimeout = setTimeout(function() {
        wheelTimeout = null;
        wheelAccum = 0;
      }, 800);
    }, { passive: false });
  }

  /* ── API Helper ────────────────────────────────────────── */
  function apiErrorMessage(detail, fallback) {
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      var messages = detail.map(function(item) {
        if (typeof item === 'string') return item;
        return item && typeof item.msg === 'string'
          ? item.msg.replace(/^Value error,\s*/, '')
          : '';
      }).filter(Boolean);
      return messages.length ? messages.join('；') : fallback;
    }
    if (detail && typeof detail === 'object') {
      if (typeof detail.message === 'string') return detail.message;
      if (typeof detail.detail === 'string') return detail.detail;
    }
    return fallback;
  }

  function apiPost(path, data) {
    return fetch(API_BASE_URL + path, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    }).then(function(res) {
      if (!res.ok) {
        return res.json().then(function(err) {
          throw new Error(apiErrorMessage(err.detail, '请求失败'));
        });
      }
      return res.json();
    });
  }

  function showModalError(msg) {
    var existing = document.querySelector('.modal-error');
    if (existing) existing.remove();
    var el = document.createElement('div');
    el.className = 'modal-error';
    el.style.cssText = 'color:#e74c3c;font-size:12px;text-align:center;margin-top:8px;padding:6px 12px;background:rgba(231,76,60,0.08);border-radius:6px';
    el.textContent = msg;
    var modal = document.querySelector('.modal');
    if (modal) modal.appendChild(el);
  }

  /* ── Password Toggle ─────────────────────────────────────── */
window.togglePassword = function(inputId, btn) {
  var inp = document.getElementById(inputId);
  if (!inp) return;
  if (inp.type === 'password') {
    inp.type = 'text';
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  } else {
    inp.type = 'password';
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  }
};
function clearModalError() {
    var existing = document.querySelector('.modal-error');
    if (existing) existing.remove();
  }

  /* ── Login Modal ──────────────────────────────────────────── */
  window.openLogin = function() {
    if (loginOverlay) {
      loginOverlay.classList.add('visible');
    }
  };

  window.closeLogin = function(event) {
    if (!loginOverlay) return;
    // 无参数调用（关闭按钮）直接关闭
    if (!event) {
      loginOverlay.classList.remove('visible');
      return;
    }
    // 点击遮罩背景层时关闭（event.target 是 overlay 本身）
    if (event.target === loginOverlay) {
      loginOverlay.classList.remove('visible');
    }
  };

  /* ── Handle Login (real API) ──────────────────────────────── */
  window.handleLogin = function(event) {
    event.preventDefault();
    clearModalError();
    var loginEl = document.getElementById('loginName');
    var passEl = document.getElementById('loginPass');
    if (!loginEl || !passEl) return;
    var login = loginEl.value.trim();
    var password = passEl.value;
    if (!login || !password) {
      showModalError('请输入用户名/邮箱和密码');
      return;
    }

    var btn = event.target.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    apiPost('/auth/login', { login: login, password: password })
      .then(function(res) {
        sessionStorage.setItem('current_user', JSON.stringify({
          user_id: res.user_id,
          username: res.username,
          role: res.role,
        }));
        window.location.href = FRONTEND_URL;
      })
      .catch(function(err) {
        showModalError(err.message || '用户名或密码错误');
        if (btn) btn.disabled = false;
      });
  };

  /* ── Handle Register ────────────────────────────────────────── */
  window.handleRegister = function(event) {
    event.preventDefault();
    clearModalError();
    var nameEl = document.getElementById('regName');
    var emailEl = document.getElementById('regEmail');
    var passEl = document.getElementById('regPass');
    if (!nameEl || !emailEl || !passEl) return;
    var username = nameEl.value.trim();
    var email = emailEl.value.trim();
    var password = passEl.value;
    if (!username || !email || !password) {
      showModalError('请填写所有字段');
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/.test(username)) {
      showModalError('用户名须为3-64位，以字母或数字开头，且只能包含字母、数字、点、下划线和连字符');
      return;
    }
    if (password.length < 6) {
      showModalError('密码至少6位');
      return;
    }

    var btn = event.target.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    apiPost('/auth/register', { username: username, password: password, email: email })
      .then(function(res) {
        if (res && res.email_verification_required) {
          // 需要邮箱验证
          window._registerEmail = email;
          document.getElementById('verifyEmailDisplay').textContent = email;
          var form = document.querySelector('#registerPanel form');
          if (form) form.style.display = 'none';
          document.getElementById('registerVerifyPanel').style.display = 'block';
          if (btn) btn.disabled = false;
        } else {
          // 无需验证，直接登录
          return apiPost('/auth/login', { login: username, password: password })
            .then(function(loginRes) {
              sessionStorage.setItem('current_user', JSON.stringify({
                user_id: loginRes.user_id,
                username: loginRes.username,
                role: loginRes.role,
              }));
              window.location.href = FRONTEND_URL;
            });
        }
      })
      .catch(function(err) {
        showModalError(err.message);
        if (btn) btn.disabled = false;
      });
  };

  /* ── Handle Forgot Password (邮件验证码) ───────────────────── */
  window.handleForgotPassword = function(event) {
    event.preventDefault();
    clearModalError();
    var emailEl = document.getElementById('forgotEmail');
    if (!emailEl) return;
    var email = emailEl.value.trim();
    if (!email) { showModalError('请输入邮箱'); return; }

    var btn = event.target.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    apiPost('/auth/forgot-password', { email: email })
      .then(function() {
        window._fpEmail = email;
        // 隐藏发送表单，显示验证码输入
        var form = document.querySelector('#forgotPasswordPanel form');
        if (form) form.style.display = 'none';
        var fpVerify = document.getElementById('fpVerifyPanel');
        if (fpVerify) fpVerify.style.display = 'block';
        if (btn) btn.disabled = false;
      })
      .catch(function(err) {
        showModalError(err.message);
        if (btn) btn.disabled = false;
      });
  };

  window.handleFpReset = function() {
    clearModalError();
    var codeEl = document.getElementById('fpVerifyCode');
    var passEl = document.getElementById('fpNewPass');
    var email = window._fpEmail;
    if (!codeEl || !passEl || !email) return;
    var code = codeEl.value.trim();
    var new_password = passEl.value;
    if (!code || code.length !== 6) { showModalError('请输入6位验证码'); return; }
    if (new_password.length < 6) { showModalError('密码至少6位'); return; }

    apiPost('/auth/reset-password', { email: email, code: code, new_password: new_password })
      .then(function() {
        alert('密码重置成功！请使用新密码登录。');
        switchAuthTab('login');
        // 恢复忘记密码面板
        var form = document.querySelector('#forgotPasswordPanel form');
        var fpVerify = document.getElementById('fpVerifyPanel');
        if (form) form.style.display = '';
        if (fpVerify) fpVerify.style.display = 'none';
        document.getElementById('fpVerifyCode').value = '';
        document.getElementById('fpNewPass').value = '';
        document.getElementById('forgotEmail').value = '';
      })
      .catch(function(err) {
        showModalError(err.message);
      });
  };

  /* ── Handle Verify Email ─────────────────────────────────── */
  window.handleVerifyEmail = function() {
    clearModalError();
    var codeEl = document.getElementById('regVerifyCode');
    var email = window._registerEmail;
    if (!codeEl || !email) return;
    var code = codeEl.value.trim();
    if (!code || code.length !== 6) {
      showModalError('请输入6位验证码');
      return;
    }

    apiPost('/auth/verify-email', { email: email, code: code })
      .then(function() {
        alert('邮箱验证成功！请切换到登录Tab进行登录。');
        // 切换回登录tab
        switchAuthTab('login');
        // 恢复注册表单
        var form = document.querySelector('#registerPanel form');
        if (form) form.style.display = '';
        document.getElementById('registerVerifyPanel').style.display = 'none';
        // 清空注册表单
        document.getElementById('regName').value = '';
        document.getElementById('regEmail').value = '';
        document.getElementById('regPass').value = '';
        document.getElementById('regVerifyCode').value = '';
        window._registerEmail = '';
      })
      .catch(function(err) {
        showModalError(err.message);
      });
  };

  /* ── Handle Resend Verification ───────────────────────────── */
  window.handleResendVerification = function() {
    clearModalError();
    var email = window._registerEmail;
    if (!email) return;

    apiPost('/auth/resend-verification', { email: email })
      .then(function() {
        alert('验证邮件已重新发送');
      })
      .catch(function(err) {
        showModalError(err.message);
      });
  };

  /* ── Auth Tab Switching ──────────────────────────────────── */
  window.switchAuthTab = function(tab) {
    var loginPanel = document.getElementById('loginPanel');
    var registerPanel = document.getElementById('registerPanel');
    var forgotPanel = document.getElementById('forgotPasswordPanel');
    var fpVerify = document.getElementById('fpVerifyPanel');
    var fpReset = document.getElementById('fpResetPanel');

    if (loginPanel) loginPanel.style.display = (tab === 'login') ? '' : 'none';
    if (registerPanel) registerPanel.style.display = (tab === 'register') ? '' : 'none';
    if (forgotPanel) forgotPanel.style.display = (tab === 'forgot') ? '' : 'none';
    if (fpVerify) fpVerify.style.display = 'none';
    if (fpReset) fpReset.style.display = 'none';

    // 恢复注册表单状态
    if (tab === 'register') {
      var form = document.querySelector('#registerPanel form');
      var verifyPanel = document.getElementById('registerVerifyPanel');
      if (form && window._registerEmail) {
        form.style.display = 'none';
        if (verifyPanel) verifyPanel.style.display = 'block';
      } else if (form) {
        form.style.display = '';
        if (verifyPanel) verifyPanel.style.display = 'none';
      }
    }
    document.querySelectorAll('.auth-tab').forEach(function(t) {
      var onclickVal = t.getAttribute('onclick') || '';
      t.classList.toggle('active', onclickVal.indexOf(tab) !== -1);
    });
    clearModalError();
  };

  /* ── Initialize ──────────────────────────────────────────── */
  updateUI(0);

})();
