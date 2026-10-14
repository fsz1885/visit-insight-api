const reportTabs=[...document.querySelectorAll('[role="tab"]')];
function selectReportTab(id,updateHash=false){
  if(!reportTabs.some(tab=>tab.id===id))return;
  reportTabs.forEach(tab=>{const active=tab.id===id;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;document.getElementById(tab.getAttribute('aria-controls')).hidden=!active});
  if(updateHash)history.replaceState(null,'','#'+id);
}
reportTabs.forEach((tab,index)=>{
  tab.addEventListener('click',()=>selectReportTab(tab.id,true));
  tab.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(index+1)%reportTabs.length;else if(event.key==='ArrowLeft')next=(index-1+reportTabs.length)%reportTabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=reportTabs.length-1;else return;event.preventDefault();selectReportTab(reportTabs[next].id,true);reportTabs[next].focus()});
});
selectReportTab(location.hash==='#tab-insights'?'tab-insights':'tab-summary');
window.addEventListener('hashchange',()=>{if(location.hash.startsWith('#tab-'))selectReportTab(location.hash.slice(1))});
document.querySelectorAll('.evidence-link').forEach(link=>link.addEventListener('click',event=>{
  const id=link.dataset.ref,target=document.getElementById('evidence-'+id);
  if(!target)return;
  event.preventDefault();
  document.getElementById('evidence-panel').open=true;
  document.querySelectorAll('.active-evidence').forEach(el=>el.classList.remove('active-evidence'));
  target.classList.add('active-evidence');
  history.replaceState(null,'','#evidence-'+id);
  target.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  target.focus({preventScroll:true});
}));
const printButton=document.getElementById('print-report');
let printDetails=[];
window.addEventListener('beforeprint',()=>{printDetails=[...document.querySelectorAll('details')].map(d=>[d,d.open]);printDetails.forEach(([d])=>d.open=true)});
window.addEventListener('afterprint',()=>printDetails.forEach(([d,open])=>d.open=open));
printButton.addEventListener('click',()=>window.print());
if(location.hash.startsWith('#evidence-')){const el=document.getElementById(location.hash.slice(1));if(el){document.getElementById('evidence-panel').open=true;el.scrollIntoView()}}
