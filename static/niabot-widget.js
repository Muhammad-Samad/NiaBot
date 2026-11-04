/*!
 * niabot-widget.js - embeds NiaBot on any website, Zendesk-widget style.
 *
 * Install by pasting this just before </body>:
 *
 *   <script src="https://niabot.naheed.pk/static/niabot-widget.js" async></script>
 *
 * Optional settings, set BEFORE the script tag:
 *
 *   <script>
 *     window.NiaBotSettings = {
 *       position: 'right',          // 'right' (default) or 'left'
 *       customerToken: '<id>.<sig>' // signed logged-in customer id (same as ?ct=)
 *     };
 *   </script>
 *
 * Control it from the host page (calls made before the script loads are queued):
 *
 *   NiaBot('open');  NiaBot('close');  NiaBot('toggle');
 *
 * The chat itself is the normal NiaBot page ("/?embed=1") in an iframe, so
 * every feature (shopping, cart, orders, complaints, uploads, memory) works
 * exactly as on niabot.naheed.pk, and the host page's CSS/JS can't affect it.
 */
(function () {
  'use strict';
  if (window.__niabotWidgetLoaded) return;
  window.__niabotWidgetLoaded = true;

  // Bot origin = wherever this script was served from.
  var script = document.currentScript
    || document.querySelector('script[src*="niabot-widget.js"]');
  var BOT_URL = new URL(script ? script.src : 'https://niabot.naheed.pk/').origin;

  var settings = window.NiaBotSettings || {};
  var side = settings.position === 'left' ? 'left' : 'right';
  var customerToken = settings.customerToken || window.NIABOT_CUSTOMER_TOKEN || '';
  var STATE_KEY = 'niabot_widget_open';

  var CSS = [
    ':host { all: initial; }',
    '.launcher {',
    '  position: fixed; bottom: 20px; ' + side + ': 20px; z-index: 2147483000;',
    '  width: 60px; height: 60px; border-radius: 50%; border: none; cursor: pointer;',
    '  background: #C8102E; color: #fff; box-shadow: 0 6px 20px rgba(200,16,46,.4);',
    '  display: flex; align-items: center; justify-content: center;',
    '  transition: transform .2s ease, background .2s ease;',
    '}',
    '.launcher:hover { background: #9E0C22; transform: scale(1.06); }',
    '.launcher svg { width: 28px; height: 28px; fill: currentColor; }',
    '.launcher .icon-close { display: none; }',
    '.open .launcher .icon-chat { display: none; }',
    '.open .launcher .icon-close { display: block; }',
    '.badge {',
    '  position: fixed; bottom: 34px; ' + side + ': 88px; z-index: 2147483000;',
    '  background: #fff; color: #1A1A2E; font: 600 13px "Segoe UI", system-ui, sans-serif;',
    '  padding: 8px 12px; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.15);',
    '  cursor: pointer; white-space: nowrap;',
    '}',
    '.open .badge, .badge[hidden] { display: none; }',
    '.frame-wrap {',
    '  position: fixed; bottom: 92px; ' + side + ': 20px; z-index: 2147483000;',
    '  width: 400px; height: 640px; max-height: calc(100vh - 112px);',
    '  border-radius: 16px; overflow: hidden; background: #F7F7F8;',
    '  box-shadow: 0 12px 40px rgba(0,0,0,.25);',
    '  opacity: 0; transform: translateY(16px) scale(.98); pointer-events: none;',
    '  visibility: hidden; transition: opacity .2s ease, transform .2s ease, visibility 0s .2s;',
    '}',
    '.open .frame-wrap {',
    '  opacity: 1; transform: none; pointer-events: auto; visibility: visible;',
    '  transition: opacity .2s ease, transform .2s ease;',
    '}',
    'iframe { width: 100%; height: 100%; border: 0; display: block; }',
    // Phones: full screen, and the widget's own ✕ closes it.
    '@media (max-width: 480px) {',
    '  .frame-wrap { inset: 0; width: 100%; height: 100%; max-height: none; border-radius: 0; }',
    '  .open .launcher { display: none; }',
    '}'
  ].join('\n');

  var CHAT_ICON = '<svg class="icon-chat" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3C6.48 3 2 6.92 2 11.75c0 2.56 1.26 4.86 3.27 6.46L4.5 21.5l3.85-1.93c1.13.36 2.35.55 3.65.55 5.52 0 10-3.92 10-8.75S17.52 3 12 3zm-4 10a1.25 1.25 0 110-2.5A1.25 1.25 0 018 13zm4 0a1.25 1.25 0 110-2.5A1.25 1.25 0 0112 13zm4 0a1.25 1.25 0 110-2.5A1.25 1.25 0 0116 13z"/></svg>';
  var CLOSE_ICON = '<svg class="icon-close" viewBox="0 0 24 24" aria-hidden="true"><path d="M18.3 5.71a1 1 0 00-1.41 0L12 10.59 7.11 5.7A1 1 0 105.7 7.11L10.59 12 5.7 16.89a1 1 0 101.41 1.41L12 13.41l4.89 4.89a1 1 0 001.41-1.41L13.41 12l4.89-4.89a1 1 0 000-1.4z"/></svg>';

  var root, frameWrap, iframe, launcher, badge;

  function build() {
    var host = document.createElement('div');
    host.id = 'niabot-widget';
    document.body.appendChild(host);
    var shadow = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

    var style = document.createElement('style');
    style.textContent = CSS;
    shadow.appendChild(style);

    root = document.createElement('div');
    shadow.appendChild(root);

    frameWrap = document.createElement('div');
    frameWrap.className = 'frame-wrap';
    root.appendChild(frameWrap);

    badge = document.createElement('div');
    badge.className = 'badge';
    badge.textContent = '👋 Need help? Chat with NiaBot';
    badge.onclick = open;
    root.appendChild(badge);

    launcher = document.createElement('button');
    launcher.className = 'launcher';
    launcher.type = 'button';
    launcher.setAttribute('aria-label', 'Open NiaBot chat');
    launcher.innerHTML = CHAT_ICON + CLOSE_ICON;
    launcher.onclick = toggle;
    root.appendChild(launcher);
  }

  // The iframe is created on first open, so the chat page (and its welcome
  // call) only loads for visitors who actually use it. Closing just hides it,
  // keeping the conversation on screen for the next open.
  function ensureFrame() {
    if (iframe) return;
    var src = BOT_URL + '/?embed=1' + (customerToken ? '&ct=' + encodeURIComponent(customerToken) : '');
    iframe = document.createElement('iframe');
    iframe.src = src;
    iframe.title = 'NiaBot chat';
    iframe.allow = 'clipboard-write';
    frameWrap.appendChild(iframe);
  }

  function setOpen(isOpen) {
    if (isOpen) ensureFrame();
    root.classList.toggle('open', isOpen);
    launcher.setAttribute('aria-label', isOpen ? 'Close NiaBot chat' : 'Open NiaBot chat');
    if (isOpen) badge.hidden = true;
    try { sessionStorage.setItem(STATE_KEY, isOpen ? '1' : '0'); } catch (e) {}
  }
  function open()   { setOpen(true); }
  function close()  { setOpen(false); }
  function toggle() { setOpen(!root.classList.contains('open')); }

  // Messages from the chat page inside the iframe (its ✕ button).
  window.addEventListener('message', function (e) {
    if (e.origin !== BOT_URL || !iframe || e.source !== iframe.contentWindow) return;
    if (e.data && e.data.type === 'niabot:close') close();
  });

  // Public API: NiaBot('open' | 'close' | 'toggle'), replaying calls queued
  // by a host page that ran before this script loaded.
  var queued = (window.NiaBot && window.NiaBot.q) || [];
  window.NiaBot = function (cmd) {
    if (cmd === 'open') open();
    else if (cmd === 'close') close();
    else if (cmd === 'toggle') toggle();
  };

  function init() {
    build();
    var wasOpen = false;
    try { wasOpen = sessionStorage.getItem(STATE_KEY) === '1'; } catch (e) {}
    if (wasOpen) open();
    queued.forEach(function (args) { window.NiaBot.apply(null, args); });
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
