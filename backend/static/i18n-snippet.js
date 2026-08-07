/*!
 * Appcovi i18n snippet — Google Translate widget + language chip.
 * Include ONCE per page via: <script src="/reader-assets/i18n-snippet.js" defer></script>
 * Adds a Language chip to the top-right, invokes Google Translate for 100+ languages,
 * remembers choice in localStorage, and sets <html lang>. Free, no API key required.
 */
(function() {
  var LANGS = [
    ['en','🇦🇺 English'],['vi','🇻🇳 Tiếng Việt'],['zh-CN','🇨🇳 中文'],
    ['pt','🇧🇷 Português'],['es','🇪🇸 Español'],['id','🇮🇩 Bahasa'],
    ['th','🇹🇭 ภาษาไทย'],['ko','🇰🇷 한국어'],['tl','🇵🇭 Filipino'],
    ['hi','🇮🇳 हिंदी'],['ar','🇸🇦 العربية'],['fr','🇫🇷 Français'],
    ['de','🇩🇪 Deutsch'],['ja','🇯🇵 日本語'],['ms','🇲🇾 Melayu']
  ];
  var KEY = 'appcovi-lang';

  function detectLang() {
    var saved = localStorage.getItem(KEY);
    if (saved) return saved;
    try {
      var fc = JSON.parse(localStorage.getItem('silo-farm-config') || '{}');
      if (fc.language) return fc.language === 'zh' ? 'zh-CN' : fc.language;
    } catch(e){}
    var br = (navigator.language || 'en').split('-')[0];
    for (var i=0;i<LANGS.length;i++) if (LANGS[i][0].split('-')[0]===br) return LANGS[i][0];
    return 'en';
  }

  function inject() {
    if (document.getElementById('appcovi-lang-chip')) return;
    var host = document.createElement('div');
    host.id = 'google_translate_element';
    host.style.cssText = 'position:absolute;left:-9999px;top:0;';
    document.body.appendChild(host);

    var chip = document.createElement('div');
    chip.id = 'appcovi-lang-chip';
    chip.style.cssText = 'position:fixed;bottom:16px;left:16px;z-index:9999;background:rgba(30,47,77,0.95);color:#C9A227;border:1px solid #C9A227;padding:8px 12px;border-radius:99px;font-weight:800;font-size:13px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,0.35);display:flex;align-items:center;gap:6px;font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;backdrop-filter:blur(8px);';
    var cur = detectLang();
    var curLbl = LANGS[0][1];
    for (var j=0;j<LANGS.length;j++) if (LANGS[j][0]===cur) { curLbl = LANGS[j][1]; break; }
    chip.innerHTML = '<span translate="no">🌐</span><span data-lbl translate="no">' + curLbl + '</span><span style="opacity:0.6;" translate="no">▾</span>';
    chip.setAttribute('translate', 'no');

    var menu = document.createElement('div');
    menu.id = 'appcovi-lang-menu';
    menu.setAttribute('translate', 'no');
    menu.style.cssText = 'position:fixed;bottom:60px;left:16px;z-index:10000;background:#fff;border:1px solid #dce3ee;border-radius:14px;padding:6px;box-shadow:0 8px 30px rgba(0,0,0,0.25);display:none;max-height:60vh;overflow-y:auto;min-width:180px;font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;';
    LANGS.forEach(function(l) {
      var it = document.createElement('div');
      it.textContent = l[1];
      it.setAttribute('translate', 'no');
      it.style.cssText = 'padding:9px 14px;font-size:14px;color:#1e2f4d;font-weight:600;cursor:pointer;border-radius:8px;';
      it.onmouseover = function(){it.style.background='#f5f7fb';};
      it.onmouseout  = function(){it.style.background='';};
      it.onclick = function() {
        localStorage.setItem(KEY, l[0]);
        setLang(l[0]);
      };
      menu.appendChild(it);
    });

    chip.onclick = function(e) {
      e.stopPropagation();
      menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
    };
    document.addEventListener('click', function(e) {
      if (!chip.contains(e.target) && !menu.contains(e.target)) menu.style.display = 'none';
    });

    document.body.appendChild(chip);
    document.body.appendChild(menu);
  }

  function setLang(code) {
    document.documentElement.lang = code;
    var host = window.location.hostname;
    var apex = host.split('.').slice(-2).join('.');
    // Clear both cookies then set fresh (path + domain scoped)
    document.cookie = 'googtrans=;path=/;expires=Thu, 01 Jan 1970 00:00:00 GMT';
    document.cookie = 'googtrans=;path=/;domain=.' + apex + ';expires=Thu, 01 Jan 1970 00:00:00 GMT';
    if (code !== 'en') {
      document.cookie = 'googtrans=/en/' + code + ';path=/';
      document.cookie = 'googtrans=/en/' + code + ';path=/;domain=.' + apex;
    }
    window.location.reload();
  }

  window.googleTranslateElementInit = function() {
    /* global google */
    if (!window.google || !google.translate) return;
    new google.translate.TranslateElement({
      pageLanguage: 'en',
      includedLanguages: LANGS.map(function(l){return l[0];}).join(','),
      layout: google.translate.TranslateElement.InlineLayout.SIMPLE,
      autoDisplay: false
    }, 'google_translate_element');
  };

  // Load Google Translate script
  var s = document.createElement('script');
  s.src = '//translate.google.com/translate_a/element.js?cb=googleTranslateElementInit';
  s.async = true;
  document.head.appendChild(s);

  document.documentElement.lang = detectLang();

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', inject);
  else inject();

  // Hide Google's default translate toolbar / banner
  var css = document.createElement('style');
  css.textContent = '.goog-te-banner-frame, .skiptranslate { display:none !important; } body { top:0 !important; } .goog-te-gadget { height:0; overflow:hidden; visibility:hidden; }';
  document.head.appendChild(css);
})();
