"""Saved host inventory uses the existing authenticated MCP management surface."""
from pathlib import Path
import shutil
import subprocess

import pytest

from src.mcp_registry import Registry
from tests.test_history_ui import UI, daemon, login  # noqa: F401
from tests.test_mcp_registry import definition


@pytest.mark.asyncio
async def test_host_inventory_requires_session_and_ui_header(daemon, tmp_path):
    http, _ = daemon
    service = http._transport.app.state.service
    service.mcp_registry = Registry(tmp_path / "library", tmp_path / "state")
    # Keep the fixture's unused supervisor intact for ordinary lifespan cleanup.
    service.mcp_proxy.status = lambda: []
    saved = service.mcp_registry.save(definition())
    service.mcp_registry.enable("example", ["codex"], saved["revision"])
    assert (await http.get("/ui/api/mcp", headers=UI)).status_code == 401
    await login(http)
    assert (await http.get("/ui/api/mcp")).status_code == 403
    response = await http.get("/ui/api/mcp", headers=UI)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    [host] = response.json()["machines"]
    assert host["current"] is True
    assert host["servers"]["example"]["apps"] == ["codex"]
    assert "state" not in host and "secret" not in host
    assert "Saved hosts" in (await http.get("/ui/mcp")).text


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_synced_endpoint_can_be_reviewed_and_confirmed_without_expanding_apps():
    page = Path(__file__).resolve().parents[1] / "src/daemon/mcp_ui.html"
    scenario = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set innerHTML(value) { throw Error('Untrusted configuration must render through textContent'); }
}
const elements = Object.fromEntries(['servers','machines','message','apps','add','transport','stdio-fields','http-fields','secret-fields','url','command','secret','alias','args','env'].map(id => [id,new Element(id)]));
elements.apps.value = 'cursor,stdio';
const document = {getElementById: id => elements[id], createElement: tag => new Element(tag)};
const changed = {alias:'example',title:'Example',status:'unconfirmed',enabled:true,apps:['codex'],
  revision:'b'.repeat(64),transport:{type:'http',url:'https://changed.test/<img/onerror=alert(1)>'}};
const disabled = {...changed,alias:'other',status:'disabled',enabled:false,apps:[]};
const data = {servers:[changed,disabled],machines:[],runtime:[]}, posts = [];
const fetch = async (url, options) => {
  assert.equal(url, '/ui/api/mcp');
  if(options.method === 'POST') {
    const body = JSON.parse(options.body); posts.push(body);
    const server = data.servers.find(item => item.alias === body.alias);
    server.status = body.apps.length ? 'ready' : 'disabled'; server.apps = body.apps;
    return {ok:true,json:async()=>({result:{}})};
  }
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
};
const descendants = element => [element,...element.children.flatMap(descendants)];
(async()=>{
  const html = fs.readFileSync(process.argv[1], 'utf8');
  const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
  await vm.runInNewContext(script, {document,fetch});
  let nodes = descendants(elements.servers.children[0]);
  assert(nodes.some(node => node.textContent === 'Endpoint: '+changed.transport.url));
  assert(nodes.some(node => node.tag === 'pre' && JSON.parse(node.textContent).revision === changed.revision));
  const confirm = nodes.find(node => node.textContent === 'Confirm this revision');
  assert(confirm); assert(nodes.some(node => node.textContent === 'Disable'));
  await confirm.onclick();
  assert.deepEqual(posts, [{action:'enable',alias:'example',apps:['codex'],revision:changed.revision}]);
  nodes = descendants(elements.servers.children[0]);
  assert(!nodes.some(node => node.textContent === 'Confirm this revision'));
  assert(nodes.some(node => node.textContent === 'Connect'));
  assert(nodes.some(node => node.textContent === 'Disable'));
  assert(descendants(elements.servers.children[1]).some(node => node.textContent === 'Enable for selected apps'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([shutil.which("node"), "-e", scenario, str(page)], capture_output=True,
                   text=True, timeout=15, check=True)
