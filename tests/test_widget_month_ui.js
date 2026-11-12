// Smoke-test Telegram HTML month bubble without browser/network.
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const html=fs.readFileSync("bot/widgets/month.html","utf8");
const script=html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);
new vm.Script(script);
class Element{
 constructor(){this.children=[];this.listeners={};this.className="";this.disabled=false}
 set textContent(v){this.text=String(v);this.children=[]}
 get textContent(){return this.text}
 appendChild(e){this.children.push(e);return e}
 addEventListener(n,cb){this.listeners[n]=cb}
 click(){return this.listeners.click?.()}
}
function load(scope=JSON.stringify({k:"monthkey"})){
 const nodes={},calls=[],start="2026-12-01";
 const document={getElementById(id){return nodes[id]??=new Element()},createElement(){return new Element()}};
 const initial={ok:1,m:"month",pg:0,pages:1,total:2,d:0,s:"p",day:-1,route:"Сочи → Стамбул",start,best:{p:7900,dt:"2026-12-08"},rows:[
  {i:0,p:7900,dt:"2026-12-08",t:1,c:"TK",tm:"09:30"},
  {i:1,p:10200,dt:"2026-12-09",t:0,c:"SU",tm:"12:10"}]};
 const W={publicData:JSON.stringify(initial),chatData:scope,ready(){calls.push("ready")},
  async request(cmd){
   calls.push(cmd);
   if(cmd.startsWith("mc:"))return{result:JSON.stringify({ok:1,cal:1,start,n:30,rows:[{i:7,p:7900},{i:8,p:10200}]})};
   if(cmd.startsWith("md:"))return{result:JSON.stringify({ok:1,detail:1,p:7900,route:"Сочи → Стамбул",dt:"2026-12-08T09:30",t:1,c:"TK",duration:null,can_open:true})};
   if(cmd.startsWith("ma:"))return{result:JSON.stringify({ok:1,message:"Ссылка в чате"})};
   return{result:JSON.stringify({...initial,d:1,day:cmd.endsWith(":7")?7:-1,rows:[initial.rows[1]],total:1})};
  }};
 vm.runInNewContext(script,{window:{TelegramWidget:W},document,Date,Math});
 return{nodes,calls}
}
(async()=>{
 const {nodes,calls}=load();
 assert.equal(nodes.items.children.length,2);
 assert.ok(calls.includes("ready"));
 assert.match(nodes.minprice.textContent,/7.*900/);
 await nodes.items.children[0].click();
 assert.ok(calls.includes("md:monthkey:0"));
 assert.ok(nodes.detail.children.length);
 await nodes.detail.children[0].children.at(-1).children[0].click();
 assert.ok(calls.includes("ma:monthkey:0"));
 nodes.detail.children[0].children.at(-1).children.at(-1).click();
 await nodes.direct.click();
 assert.ok(calls.includes("mt:monthkey:1"));
 await nodes.calendar.click();
 assert.ok(calls.includes("mc:monthkey:1"));
 assert.equal(nodes.cal.children.length,30);
 assert.equal(nodes.cal.children[7].disabled,false);
 await nodes.cal.children[7].click();
 assert.ok(calls.includes("ml:monthkey:0:1:p:7"));
 assert.equal(nodes.reset.className,"chip");
 assert.equal(nodes.bestlabel.textContent,"Минимальная цена на дату");
 await nodes.reset.click();
 assert.ok(calls.includes("ml:monthkey:0:1:p:-1"));
 assert.equal(nodes.reset.className,"chip hide");
 assert.equal(nodes.bestlabel.textContent,"Минимальная цена за месяц");
 const withoutScope=load(null);
 await withoutScope.nodes.direct.click();
 assert.ok(!withoutScope.calls.some(x=>x.startsWith("mt:")));
 console.log("Native monthly fares HTML bubble smoke test passed");
})().catch(e=>{console.error(e);process.exitCode=1});
