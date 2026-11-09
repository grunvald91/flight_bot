// Telegram HTML bubble visual smoke test without real network.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const html = fs.readFileSync("bot/widgets/offers.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);
new vm.Script(script);

class Element {
 constructor() {
  this.children = []; this.listeners = {}; this.className = ""; this.disabled = false;
  this.classList = {add:()=>{},remove:()=>{}};
 }
 set textContent(v) {this.text=String(v);this.children=[]}
 get textContent(){return this.text}
 appendChild(el){this.children.push(el);return el}
 addEventListener(k,fn){this.listeners[k]=fn}
 click(){return this.listeners.click?.()}
}
function init(scope='{"k":"sample"}') {
 const nodes = {}, calls=[];
 const document={
  getElementById(id){return nodes[id]??=new Element()},
  createElement(){return new Element()},
 };
 const initial={ok:1,m:"discover",pg:0,pages:1,total:2,d:0,s:"p",dt:"2026-12-23",unverified:1,rows:[
  {i:0,a:"Армения",b:"Ереван",p:12000,s:0,t:"2026-12-23 11:20",c:"SU"},
  {i:1,a:"Турция",b:"Стамбул",p:8000,s:1,t:"2026-12-23 18:00",c:"TK"},
 ]};
 const W={chatData:scope,publicData:JSON.stringify(initial),
  ready(){calls.push("ready")},openLink(url){calls.push("link:"+url)},
  async request(req){
   calls.push(req);
   if(req.startsWith("od:"))return {result:JSON.stringify({
    ok:1,detail:1,m:"discover",a:"Армения",b:"Ереван",p:12000,s:0,depart:"2026-12-23T11:20",
    arrive:"2026-12-23T13:20",airline:"SU",duration:120,bag:0,bagkg:null,hand:1,
    fare:"Эконом",refund:false,change:null,visa:true,checkout:true
   })};
   if(req.startsWith("ov:"))return {result:JSON.stringify({
    ok:1,name:"Армения",summary:"Правила въезда",url:"https://example.org"
   })};
   if(req.startsWith("oa:"))return {result:JSON.stringify({ok:1,message:"Кнопки отправлены"})};
   return {result:JSON.stringify({...initial, d:1,total:1,
    rows:[initial.rows[0]]})};
  }};
 vm.runInNewContext(script,{window:{TelegramWidget:W},document});
 return {nodes,calls};
}
(async()=>{
 const {nodes,calls}=init();
 assert.equal(nodes.list.children.length,2);
 assert.ok(calls.includes("ready"));
 await nodes.list.children[0].click();
 assert.equal(nodes.detail.children[1].textContent,"Армения → Ереван");
 assert.ok(calls.includes("od:sample:0"));
 const actions=nodes.detail.children.at(-1);
 assert.equal(actions.children.length,3);
 await actions.children[0].click();
 assert.ok(calls.includes("oa:sample:0"));
 await actions.children[1].click();
 assert.ok(calls.includes("ov:sample:0"));
 const visaActions=nodes.detail.children.at(-1);
 visaActions.children[0].click();
 assert.ok(calls.includes("link:https://example.org"));
 await visaActions.children[1].click();
 nodes.detail.children.at(-1).children.at(-1).click();
 assert.equal(nodes.list.children.length,2);
 await nodes.direct.click();
 assert.ok(calls.includes("of:sample:0:1:p"));
 assert.equal(nodes.list.children.length,1);
 await nodes.stops.click();
 assert.ok(calls.includes("of:sample:0:1:s"));
 const noScope=init(null);
 await noScope.nodes.direct.click();
 assert.ok(!noScope.calls.some(x=>x.startsWith("of:")));
 console.log("Verified flight offers HTML bubble UI smoke test passed");
})().catch(e=>{console.error(e);process.exitCode=1});
