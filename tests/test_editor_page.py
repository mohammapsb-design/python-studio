"""Tests for the Monaco page (assets/editor.html).

Static checks always run. The behavioural checks execute the page's real <script> under
Node against a *stub* of the `monaco` global: they verify this project's glue (bridge
messages, dirty tracking, stale-text protection, completion provider, diagnostics,
disposal). They do NOT prove that real Monaco renders or works inside Termux:GUI."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from python_studio.bridge import ALLOWED_OPS, Controller
from python_studio.files import Document
from python_studio.monaco_bundle import AssetBundle

ASSETS = os.path.join(os.path.dirname(__file__), "..", "src", "python_studio", "assets")
HTML = os.path.join(ASSETS, "editor.html")
BASIC = os.path.join(ASSETS, "basic_editor.html")
HAS_NODE = shutil.which("node") is not None


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def script_of(html):
    return re.search(r"<script>(.*)</script>", html, re.S).group(1)


class StaticTest(unittest.TestCase):
    def setUp(self):
        self.html = read(HTML)

    def test_no_external_urls_cdn_or_http(self):
        self.assertIsNone(re.search(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", self.html))
        self.assertEqual(re.findall(r"https?://[^\s\"')]+", self.html), [])
        self.assertNotIn("<script src", self.html)

    def test_no_worker_or_blob_or_url_loading(self):
        """The page is a setData() page: no Worker/Blob/object-URL tricks, no fetch/XHR, no script src loading."""
        for needle in ("new Worker(", "createObjectURL", "new Blob", "fetch(", "XMLHttpRequest",
                       "importScripts", "location.href", "setAttribute(\"src\"", ".src="):
            self.assertNotIn(needle, self.html, needle)
        self.assertIn("getWorker:function(){", self.html)

    def test_old_editor_is_gone_from_primary_page(self):
        for needle in ("<textarea", "PSCore", 'id="gutter"', 'id="pop"', "contenteditable"):
            self.assertNotIn(needle, self.html)

    def test_single_editor_instance_created(self):
        self.assertEqual(self.html.count("monaco.editor.create("), 1)

    def test_python_language_and_core_options(self):
        for needle in ('language:"python"', "tabSize:4", "insertSpaces:true", 'autoIndent:"full"',
                       'autoClosingBrackets:"languageDefined"', 'autoClosingQuotes:"languageDefined"',
                       "folding:true", 'renderLineHighlight:"all"', 'lineNumbers:"on"',
                       "bracketPairColorization", 'matchBrackets:"always"', "automaticLayout:true"):
            self.assertIn(needle, self.html)

    def test_amd_only_no_esm_mixing(self):
        self.assertIn("vs/editor/editor.main", self.html)
        self.assertNotIn("import(", self.html)
        self.assertNotIn('type="module"', self.html)

    def test_bridge_ops_sent_are_all_allowed(self):
        sent = set(re.findall(r"send\(\{op:\"(\w+)\"", self.html))
        self.assertTrue(sent)
        self.assertLessEqual(sent, ALLOWED_OPS)
        self.assertEqual(sent, {"ready", "save", "run", "stop", "exit"})

    def test_both_pages_implement_every_python_to_js_call(self):
        called = {"load", "saved", "runStarted", "out", "runEnded", "fail", "back", "notice"}
        self.assertRegex(read(HTML), r"assetsBegin:function")
        self.assertRegex(read(HTML), r"assetChunk:function")
        self.assertRegex(read(HTML), r"assetsEnd:function")
        for path in (HTML, BASIC):
            src = read(path)
            for fn in called:
                self.assertRegex(src, r"\b%s:function" % fn, (path, fn))

    def test_uses_model_value_not_a_second_buffer(self):
        self.assertIn("model.getValue(monaco.editor.EndOfLinePreference.LF)", self.html)
        self.assertNotIn("ta.value", self.html)

    def test_page_is_ascii(self):
        self.html.encode("ascii")


NODE_HARNESS = r"""
const vm=require('vm'),fs=require('fs');
const scenario=process.argv[2], src=fs.readFileSync(process.argv[3],'utf8'), calls=JSON.parse(fs.readFileSync(process.argv[4],'utf8'));
const script=/<script>([\s\S]*)<\/script>/.exec(src)[1];
const bridge=[],log=[];
function el(id){const e={id,style:{},className:'',children:[],listeners:{},disabled:false,_t:'',
  get textContent(){return this._t},set textContent(v){this._t=v;this.children=[]},
  appendChild(c){this.children.push(c)},addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f)},
  setAttribute(k,v){this.attrs=this.attrs||{};this.attrs[k]=v},dispatchEvent(ev){this.fire(ev.type)},
  fire(t){(this.listeners[t]||[]).forEach(f=>f({preventDefault(){}}))},click(){this.fire('pointerdown');this.fire('click')},
  text(){return this.children.length?this.children.map(c=>c.textContent).join(''):this._t}};return e}
const els={},heads=[];
const doc={getElementById(i){return els[i]||(els[i]=el(i))},createElement(t){const e=el(t);e.tag=t;return e},
  createTextNode(t){return{textContent:t}},querySelector(){return null},activeElement:{blur(){log.push('blur')}},
  head:{appendChild(c){heads.push(c)}}};
class FakeModel{constructor(t,l,u){this.t=t;this.lang=l;this.uri=u;this.alt=1;this.hist=[];this.cb=[];this.disposed=false;this.eol=null}
  getValue(p){return this.t}setValue(t){this.t=t;this.alt++;this.cb.forEach(f=>f())}getAlternativeVersionId(){return this.alt}
  getValueLength(){return this.t.length}onDidChangeContent(f){this.cb.push(f);return{dispose(){}}}setEOL(e){this.eol=e}updateOptions(){}
  dispose(){this.disposed=true}
  getWordUntilPosition(p){const line=this.t.split('\n')[p.lineNumber-1].slice(0,p.column-1);const m=/\w*$/.exec(line)[0];
    return{word:m,startColumn:p.column-m.length,endColumn:p.column}}
  getLineContent(n){return this.t.split('\n')[n-1]}
  edit(s){this.hist.push([this.t,this.alt]);this.t+=s;this.alt++;this.cb.forEach(f=>f())}
  undo(){const [t,a]=this.hist.pop();this.t=t;this.alt=a;this.cb.forEach(f=>f())}}
const created={editors:0,models:[],providers:[],options:null,triggers:[],commands:0,disposedEditor:false,theme:null};
function makeMonaco(){return {editor:{
  create(node,opts){created.editors++;created.options=opts;return{setModel(m){this.m=m},addCommand(){created.commands++},
    setScrollPosition(){},setPosition(){},focus(){},layout(){},hasTextFocus(){return true},
    trigger(s,a){created.triggers.push(a)},dispose(){created.disposedEditor=true}}},
  createModel(t,l,u){const m=new FakeModel(t,l,u);created.models.push(m);return m},defineTheme(n){created.theme=n},
  EndOfLinePreference:{LF:1},EndOfLineSequence:{LF:0}},
  languages:{getLanguages(){return scenario==='nolang'?[{id:'plaintext'}]:[{id:'python'}]},
    registerCompletionItemProvider(l,p){created.providers.push([l,p]);return{dispose(){created.providerDisposed=true}}},
    CompletionItemKind:{Variable:4,Keyword:17,Function:1,Class:5},CompletionTriggerKind:{Invoke:0,TriggerCharacter:1}},
  KeyMod:{CtrlCmd:2048},KeyCode:{KeyS:49,Enter:3},Uri:{from(o){return o}}}}
let workers=0;
const ctx={document:doc,console:{log(m){log.push(m);if(String(m).startsWith('PSBRIDGE:'))bridge.push(JSON.parse(m.slice(9)))}},
  setTimeout,clearTimeout,atob,TextDecoder,encodeURIComponent,Event:class{constructor(t){this.type=t}},__scn:scenario,PS_STALL_MS:60,
  Worker:class{constructor(){workers++}},
  innerWidth:400,innerHeight:800,winl:{},__makeMonaco:makeMonaco,
  addEventListener(t,f){(ctx.winl[t]=ctx.winl[t]||[]).push(f)}};
ctx.window=ctx;vm.createContext(ctx);vm.runInContext(script,ctx);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const $=i=>doc.getElementById(i);
const W=(t,f)=>(ctx.winl[t]||[]).forEach(x=>x(f));
(async()=>{
  const R={};
  R.readyAfterBoot=bridge.slice();
  R.pendingFirst=calls.length&&calls[0].startsWith('PS.load(');
  for(const c of calls){vm.runInContext(c,ctx)}   // exactly what Termux:GUI's evaluateJS does
  if(R.pendingFirst)R.beforeInitName=$('fname').text();
  await sleep(150);
  R.edmsg={text:$('edmsg').textContent,display:$('edmsg').style.display,cls:$('edmsg').className};
  R.disabled=['run','save','tab','undo','redo','find'].map(i=>$(i).disabled);
  R.statusText=$('constatus').textContent;
  R.outText=$('out').children.map(c=>c.textContent).join('');
  R.created={editors:created.editors,models:created.models.length,theme:created.theme,commands:created.commands};
  R.cfg=ctx.__cfg;R.pyAfter=ctx.__pyAfterMonaco;R.utf8=ctx.__u;R.envAtLoad=ctx.__envAtLoad;R.workers=workers;
  R.link=heads.filter(h=>h.tag==='link'&&h.attrs&&h.attrs['data-name']).map(h=>({name:h.attrs['data-name'],href:h.attrs.href.slice(0,40),full:h.attrs.href}));
  try{ctx.MonacoEnvironment.getWorker('x','y');R.getWorkerThrows=false}catch(e){R.getWorkerThrows=String(e.message)}
  R.workersAfter=workers;
  if(created.options)R.options={lang:created.options.language,tab:created.options.tabSize,eow:created.options.acceptSuggestionOnEnter,model:created.options.model};
  if(['main','pendingload','dupready','linkreq'].includes(scenario)&&created.models.length){
    const m=created.models[0];
    R.model={text:m.t,lang:m.lang,uri:m.uri,eol:m.eol};
    R.name0=$('fname').text();
    $('run').click();R.run0=bridge[bridge.length-1];
    ctx.PS.runStarted('a b.py');R.runHidden=$('run').style.display;R.blurred=log.includes('blur');
    ctx.PS.runEnded(0,'exit code 0');
    m.edit('x=2\n');R.nameDirty=$('fname').text();
    $('save').click();R.save1=bridge[bridge.length-1];
    m.edit('y=3\n');
    ctx.PS.saved(true,'saved a b.py');R.dirtyAfterRacySave=$('fname').text();
    $('save').click();ctx.PS.saved(true,'saved');R.cleanAfterSave=$('fname').text();
    m.edit('z');R.d1=$('fname').text();m.undo();R.undoClean=$('fname').text();
    ctx.PS.saved(false,'disk full');R.afterFail=$('fname').text();
    m.edit('q');const before=bridge.length;$('exit').click();R.modal=$('modal').style.display;R.exitSentWhileDirty=bridge.length>before;
    $('m-disc').click();R.discard=bridge[bridge.length-1];
    ctx.PS.back();R.backModal=$('modal').style.display;ctx.PS.back();R.backClosed=$('modal').style.display;
    $('undo').click();$('redo').click();$('find').click();$('tab').click();R.triggers=created.triggers.slice();
    ctx.PS.load('OTHER','a b.py',true);R.reloadKept=m.t.includes('OTHER');
    ctx.PS.notice('hello');R.noticeShown=$('out').children.some(c=>c.textContent.includes('[notice] hello'));
    const prov=created.providers.find(p=>p[0]==='python')[1];
    const lbl=(text,line,col,ctxo)=>{m.t=text;const r=prov.provideCompletionItems(m,{lineNumber:line,column:col},ctxo||{triggerKind:1});
      return r.suggestions.map(s=>s.label)};
    R.cPri=lbl('pri',1,4);R.cDoc=lbl('def calculate_total():\n    pass\ncalc',3,5);R.cKw=lbl('    ret',1,8);
    R.cDot=lbl('os.pri',1,7);R.cShort=lbl('p',1,2);R.cShortExplicit=lbl('p',1,2,{triggerKind:0}).includes('print');
    R.cSelf=lbl('my_unique_name',1,15);R.cNum=lbl('12',1,3);
    m.t='pri';R.range=prov.provideCompletionItems(m,{lineNumber:1,column:4},{triggerKind:1}).suggestions[0].range;
    R.ext=typeof ctx.PSEditor.registerCompletionProvider;ctx.PSEditor.registerCompletionProvider({provideCompletionItems(){return{suggestions:[]}}});
    R.providersAfterExt=created.providers.length;
    W('pagehide');R.disposed={editor:created.disposedEditor,model:m.disposed,provider:!!created.providerDisposed};
  }
  if(scenario==='keyboard'){
    ctx.innerHeight=380;W('resize');R.kb=$('app').className;ctx.innerHeight=800;W('resize');R.kbOff=$('app').className;
    ctx.innerWidth=800;ctx.innerHeight=380;W('resize');R.landscape=$('app').className;
  }
  if(['corrupt','missing_module','nolang','truncated','scriptreq','stall'].includes(scenario)){
    $('run').click();$('save').click();R.opsAfterClicks=bridge.slice(1).map(x=>x.op);
    ctx.PS.load('print(1)\n','a.py',true);
  }
  console.error(JSON.stringify(R));process.exit(0);
})().catch(e=>{console.error('HARNESS ERROR '+e.stack);process.exit(1)});
"""


FAKE_LOADER = """(function(g){var mods={};
g.define=function(n,d,f){mods[n]={d:d,f:f,done:false};if(n.indexOf('vs/basic-languages/')===0){mods[n].done=true;f()}};
function load(n){var m=mods[n];if(!m){var e=new Error('no module '+n);e.moduleId=n;throw e}
  if(!m.done){m.done=true;m.d.forEach(load);m.f()}}
g.require=function(deps,ok,bad){
  if(g.__scn==='stall')return;                       // never calls back, requests nothing
  if(g.__scn==='scriptreq'){var s=document.createElement('script');
    s.addEventListener('error',function(){bad(new Error('Could not load script'))});
    s.setAttribute('src','file:///ps-bundled/vs/editor/editor.main.nls.js');return}
  var go=function(){try{deps.forEach(load)}catch(e){bad(e);return}ok()};
  if(g.__scn==='linkreq'){var l=document.createElement('link');l.addEventListener('load',go);
    l.setAttribute('href','file:///ps-bundled/vs/editor/editor.main.css');return}
  go()};
g.require.config=function(c){g.__cfg=c};
})(this);
"""
FAKE_MAIN = ('define("vs/editor/editor.main",%s,function(){globalThis.__envAtLoad=!!globalThis.MonacoEnvironment;'
             'globalThis.monaco=globalThis.__makeMonaco();});globalThis.__u="\u00e9";\n')
FAKE_CSS = "@font-face{font-family:codicon;src:url(../base/browser/ui/codicons/codicon/codicon.ttf?x) format('truetype')}\n"


def make_assets(root, missing_dep=False):
    files = {
        "vs/loader.js": FAKE_LOADER,
        "vs/editor/editor.main.js": FAKE_MAIN % ('["vs/nope"]' if missing_dep else "[]"),
        "vs/basic-languages/python/python.js": 'define("vs/basic-languages/python/python",[],function(){if(!globalThis.monaco)throw new Error("Synchronous require cannot resolve module vs/editor/editor.api");globalThis.__pyAfterMonaco=true;});\n',
        "vs/editor/editor.main.css": FAKE_CSS,
        "vs/base/browser/ui/codicons/codicon/codicon.ttf": "FONTBYTES",
    }
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(text.encode("utf-8"))


def real_calls(scenario):
    """The exact JS statements the real Controller sends on 'ready' (Monaco bundle + document)."""
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "monaco")
        make_assets(root, missing_dep=(scenario == "missing_module"))
        path = os.path.join(d, "a b.py")
        with open(path, "w") as fh:
            fh.write("print(1)\n")
        sent = []
        ctl = Controller(Document.open(path), eval_js=sent.append, finish=lambda: None,
                         assets=AssetBundle.build(root))
        ctl.handle('PSBRIDGE:{"op":"ready"}')
        ctl.shutdown()
    calls = list(sent)
    if scenario == "dupready":
        calls = calls[:-1] + calls[:-1] + calls[-1:]            # assets pushed twice, then the document
    if scenario == "pendingload":
        calls = [calls[-1]] + calls[:-1]                        # document arrives before Monaco
    if scenario == "corrupt":
        i = next(k for k, c in enumerate(calls) if c.startswith("PS.assetChunk(2,"))
        raw = bytearray(base64.b64decode(re.search(r'"([A-Za-z0-9+/=]+)"\);$', calls[i]).group(1)))
        raw[3] ^= 0x01
        calls[i] = re.sub(r'"[A-Za-z0-9+/=]+"\);$', '"%s");' % base64.b64encode(bytes(raw)).decode(), calls[i])
    if scenario == "truncated":
        i = next(k for k, c in enumerate(calls) if c.startswith("PS.assetChunk(2,"))
        del calls[i]
    return calls


def run_node(scenario):
    """Replays the real Controller output into the real page script under Node (fake Monaco)."""
    calls = real_calls(scenario)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(NODE_HARNESS)
        js = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(calls, fh)
        cj = fh.name
    try:
        p = subprocess.run(["node", js, scenario, HTML, cj], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(js)
        os.unlink(cj)
    if p.returncode != 0 or "HARNESS ERROR" in p.stderr:
        raise AssertionError(p.stderr)
    return json.loads(p.stderr.strip().splitlines()[-1])


@unittest.skipUnless(HAS_NODE, "node not installed")
class PageBehaviourTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = run_node("main")
        cls.html_text = read(HTML)

    def test_syntax_valid(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script_of(read(HTML)))
        try:
            subprocess.run(["node", "--check", fh.name], check=True, capture_output=True)
        finally:
            os.unlink(fh.name)

    def test_ready_sent_once_and_editor_created_once(self):
        R = self.R
        self.assertEqual(R["readyAfterBoot"], [{"op": "ready"}])
        self.assertEqual(R["created"]["editors"], 1)
        self.assertEqual(R["created"]["models"], 1)
        self.assertEqual(R["created"]["theme"], "ps-dark")
        self.assertEqual(R["statusText"], "Console")

    def test_editor_configured_for_python_without_initial_model(self):
        o = self.R["options"]
        self.assertEqual((o["lang"], o["tab"], o["eow"], o["model"]), ("python", 4, "off", None))

    def test_document_loaded_into_python_model(self):
        m = self.R["model"]
        self.assertEqual(m["text"], "print(1)\n")
        self.assertEqual(m["lang"], "python")
        self.assertEqual(m["uri"], {"scheme": "file", "path": "/a b.py"})
        self.assertEqual(m["eol"], 0)
        self.assertEqual(self.R["name0"], "a b.py")

    def test_run_and_save_send_current_model_text(self):
        self.assertEqual(self.R["run0"], {"op": "run", "text": "print(1)\n"})
        self.assertEqual(self.R["save1"], {"op": "save", "text": "print(1)\nx=2\n", "then_exit": False})

    def test_run_started_hides_run_and_closes_keyboard(self):
        self.assertEqual(self.R["runHidden"], "none")
        self.assertTrue(self.R["blurred"])

    def test_dirty_tracking(self):
        R = self.R
        self.assertEqual(R["nameDirty"], "* a b.py")
        self.assertEqual(R["dirtyAfterRacySave"], "* a b.py")   # edit made while saving stays dirty
        self.assertEqual(R["cleanAfterSave"], "a b.py")
        self.assertEqual(R["d1"], "* a b.py")
        self.assertEqual(R["undoClean"], "a b.py")              # undo back to the saved text clears it
        self.assertEqual(R["afterFail"], "a b.py")              # failed save does not change state

    def test_exit_prompts_when_dirty(self):
        R = self.R
        self.assertEqual(R["modal"], "flex")
        self.assertFalse(R["exitSentWhileDirty"])
        self.assertEqual(R["discard"], {"op": "exit"})
        self.assertEqual((R["backModal"], R["backClosed"]), ("flex", "none"))  # back: first closes? see below

    def test_toolbar_drives_monaco_commands(self):
        self.assertEqual(self.R["triggers"], ["undo", "redo", "actions.find", "tab"])

    def test_dirty_editor_refuses_reload_and_notice_works(self):
        self.assertTrue(self.R["reloadKept"] is False)
        self.assertTrue(self.R["noticeShown"])

    def test_completion_provider(self):
        R = self.R
        self.assertEqual(R["cPri"][0], "print")
        self.assertEqual(R["cDoc"][0], "calculate_total")
        self.assertIn("return", R["cKw"])
        self.assertEqual(R["cDot"], [])
        self.assertEqual(R["cShort"], [])
        self.assertTrue(R["cShortExplicit"])
        self.assertEqual(R["cSelf"], [])
        self.assertEqual(R["cNum"], [])
        self.assertEqual(R["range"], {"startLineNumber": 1, "endLineNumber": 1, "startColumn": 1, "endColumn": 4})

    def test_extension_point_for_future_providers(self):
        self.assertEqual(self.R["ext"], "function")
        self.assertEqual(self.R["providersAfterExt"], 2)

    def test_resources_disposed_on_pagehide(self):
        self.assertEqual(self.R["disposed"], {"editor": True, "model": True, "provider": True})

    def test_bundle_delivered_through_bridge_and_executed(self):
        R = self.R
        self.assertEqual(R["cfg"]["paths"]["vs"], "file:///ps-bundled/vs")   # unreachable on purpose: no URL loading
        self.assertTrue(R["envAtLoad"])                                      # MonacoEnvironment set before Monaco runs
        self.assertEqual(R["created"]["editors"], 1)
        self.assertEqual(R["utf8"], "\u00e9")                                # non-ASCII source survives the base64/UTF-8 trip

    def test_language_file_runs_only_after_monaco_is_up(self):
        """Regression: python.js needs vs/editor/editor.api at define() time (real-device error)."""
        self.assertTrue(self.R["pyAfter"])

    def test_loader_script_request_is_reported_and_fails_fast(self):
        R = run_node("scriptreq")
        self.assertIn("[loader asked for script] file:///ps-bundled/vs/editor/editor.main.nls.js", R["outText"])
        self.assertIn("Could not load script", R["edmsg"]["text"])
        self.assertEqual(R["disabled"], [True] * 6)

    def test_loader_stylesheet_request_is_answered_so_loader_does_not_wait_forever(self):
        R = run_node("linkreq")
        self.assertIn("[loader asked for link] file:///ps-bundled/vs/editor/editor.main.css", R["outText"])
        self.assertEqual(R["created"]["editors"], 1)          # it continued and Monaco started

    def test_silent_stall_becomes_a_diagnostic_not_a_hang(self):
        R = run_node("stall")
        self.assertEqual(R["edmsg"]["cls"], "fatal")
        self.assertIn("loader did not finish", R["edmsg"]["text"])
        self.assertIn("none", R["edmsg"]["text"])
        self.assertEqual(R["opsAfterClicks"], [])

    def test_static_loader_safety_options(self):
        self.assertIn("catchError:true", self.html_text)
        self.assertIn("require.onError", self.html_text)

    def test_css_arrives_as_data_link_with_font_inlined(self):
        links = self.R["link"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["name"], "vs/editor/editor.main")
        self.assertTrue(links[0]["href"].startswith("data:text/css"))
        self.assertIn("data%3Afont%2Fttf%3Bbase64%2C", links[0]["full"])      # codicon font embedded, no relative url left

    def test_workers_never_created_and_getworker_throws_for_main_thread_fallback(self):
        R = self.R
        self.assertEqual((R["workers"], R["workersAfter"]), (0, 0))
        self.assertIn("Web Workers are not available", R["getWorkerThrows"])
        self.assertIn("without Web Workers", R["outText"])

    def test_document_before_monaco_is_applied_when_monaco_starts(self):
        R = run_node("pendingload")
        self.assertTrue(R["pendingFirst"])
        self.assertEqual(R["beforeInitName"], "a b.py")
        self.assertEqual(R["model"]["text"], "print(1)\n")
        self.assertEqual(R["created"]["models"], 1)

    def test_repeated_ready_does_not_start_monaco_twice(self):
        R = run_node("dupready")
        self.assertEqual(R["created"]["editors"], 1)
        self.assertEqual(R["model"]["text"], "print(1)\n")

    def test_keyboard_collapses_console_but_rotation_does_not(self):
        R = run_node("keyboard")
        self.assertEqual((R["kb"], R["kbOff"], R["landscape"]), ("kb", "", ""))

    def test_failures_show_diagnostics_and_block_save_run(self):
        for scenario, needle in (("corrupt", "checksum mismatch"), ("truncated", "never arrived"),
                                 ("missing_module", "no module vs/nope"), ("nolang", "no 'python' language")):
            R = run_node(scenario)
            self.assertEqual(R["edmsg"]["cls"], "fatal", scenario)
            self.assertEqual(R["edmsg"]["display"], "block", scenario)
            self.assertIn("Monaco could not start", R["edmsg"]["text"])
            self.assertIn(needle, R["edmsg"]["text"], scenario)
            self.assertIn("--basic-editor", R["edmsg"]["text"])
            self.assertEqual(R["disabled"], [True] * 6, scenario)
            self.assertEqual(R["opsAfterClicks"], [], scenario)   # nothing can overwrite the file
            self.assertEqual(R["statusText"], "editor failed to start")


if __name__ == "__main__":
    unittest.main()
