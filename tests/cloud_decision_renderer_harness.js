// Execute the shipped app.js against fixture fetches and a text-only DOM.
// Renderer logic is not substituted; no browser/provider network is available.
const fs=require('node:fs');
const vm=require('node:vm');
const request=JSON.parse(fs.readFileSync(0,'utf8'));
const escape=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.ownText='';this.listeners=[];this.id='';this.className='';this.parent=null;}
  set textContent(value){this.ownText=value===undefined?'':String(value);this.children=[];}
  get textContent(){return this.ownText+this.children.map(child=>child.textContent).join('');}
  append(...children){for(const child of children){if(!(child instanceof Element))throw Error('Unexpected DOM insertion');child.parent=this;this.children.push(child);}}
  replaceChildren(...children){this.ownText='';this.children=[];this.append(...children);}
  after(child){const index=this.parent.children.indexOf(this);child.parent=this.parent;this.parent.children.splice(index+1,0,child);}
  addEventListener(type,callback){this.listeners.push({type,callback});}
  find(id){if(this.id===id)return this;for(const child of this.children){const found=child.find(id);if(found)return found;}return null;}
  serialize(){const attrs=(this.id?' id="'+escape(this.id)+'"':'')+(this.className?' class="'+escape(this.className)+'"':'');return '<'+this.tag+attrs+'>'+escape(this.ownText)+this.children.map(child=>child.serialize()).join('')+'</'+this.tag+'>';}
}
const root=new Element('main');
for(const [tag,id] of [['form','scan'],['button','refresh'],['div','scans'],['div','launches'],['div','paper'],['p','message'],['input','mint']]){const element=new Element(tag);element.id=id;root.append(element);}
const document={createElement:tag=>new Element(tag),querySelector:selector=>root.find(selector.slice(1))};
const data=request.data;
if(request.special_observed){
  const values={nan:NaN,infinity:Infinity,unsafe:Number.MAX_SAFE_INTEGER+1,out_of_date_range:8640000000001};
  data.decisions[0].observed_at=values[request.special_observed];
}
let clock=request.now_ms??1000000;
if(request.special_clock)clock=request.special_clock==='nan'?NaN:-1;
class FixtureDate extends Date {constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
let fetchOK=request.fetch_ok!==false;
const calls=[],intervals=[];
const context={document,Date:FixtureDate,console,setInterval:(callback,ms)=>intervals.push({callback,ms}),fetch:async(url,options)=>{
  calls.push({url,method:options?.method||'GET'});
  const payload=url==='/api/decisions'?data:url==='/api/scans'?(request.scans_data??[]):url==='/api/launches'?{launches:[]}:(request.paper_data??{status:'NOT_CONFIGURED'});
  return {ok:url==='/api/decisions'?fetchOK:true,json:async()=>payload};
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context,{filename:'desk/static/app.js'});
(async()=>{
  await context.paper();
  await context.refresh();
  await context.decisions();
  const first=root.find('entry-decisions').serialize();
  if(request.next_now_ms!==undefined||request.next_fetch_ok!==undefined){
    if(request.next_now_ms!==undefined)clock=request.next_now_ms;
    if(request.next_fetch_ok!==undefined)fetchOK=request.next_fetch_ok;
    await context.decisions();
  }
  const section=root.find('entry-decisions');
  const paragraphs=section.children.flatMap(child=>child.tag==='details'?child.children.filter(n=>n.tag==='p').map(n=>n.textContent):child.tag==='p'?[child.textContent]:[]);
  const summaries=section.children.filter(child=>child.tag==='details').map(child=>child.children.find(n=>n.tag==='summary').textContent);
  process.stdout.write(JSON.stringify({html:section.serialize(),paper_html:root.find('paper').serialize(),scans_html:root.find('scans').serialize(),first_html:first,paragraphs,summaries,calls,
    intervals:intervals.map(i=>i.ms),controls:{submit_listeners:root.find('scan').listeners.length,refresh_listeners:root.find('refresh').listeners.length}}));
})().catch(error=>{console.error(error);process.exitCode=1;});
