// Fixed optional MV3 adapter. Content scripts never perform network requests.
importScripts("core.js");
chrome.storage.session.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"});
let pendingDraft=null;
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(message?.type!=="take-selected-preview"||sender.id!==chrome.runtime.id||sender.url!==chrome.runtime.getURL("preview.html"))return;
  const draft=pendingDraft&&Date.now()<pendingDraft.expires_at?pendingDraft:null;pendingDraft=null;respond({draft});
});
chrome.runtime.onInstalled.addListener(()=>chrome.contextMenus.create({id:"selected-text",title:"Review selected text privately for Seraph",contexts:["selection"]}));
async function capture(tab){
  try{
    if(!tab?.id||tab.incognito)throw Error("Ordinary non-incognito tab required");
    const results=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},func:()=>{
      if(window!==window.top||!/^https?:$/.test(location.protocol)||document.contentType!=="text/html"||document.querySelector("embed[type='application/pdf'],object[type='application/pdf']"))throw Error("Unsupported document");
      if(location.protocol==="http:"&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname))throw Error("Insecure origin unsupported");
      const selection=getSelection();if(!selection||selection.rangeCount!==1||selection.isCollapsed)throw Error("Select ordinary text first");
      const range=selection.getRangeAt(0);let count=0;
      function protectedNode(node){
        let el=node.nodeType===1?node:node.parentElement;
        for(;el;el=el.parentElement){
          const style=getComputedStyle(el);
          if(el.getRootNode()!==document||el.tagName.includes('-')||['FORM','INPUT','TEXTAREA','SELECT','BUTTON','OPTION'].includes(el.tagName)||el.isContentEditable||el.hasAttribute('contenteditable')||el.closest('[inert],[hidden],[aria-hidden="true"]')||el.hasAttribute('autocomplete')||style.display==='none'||style.visibility!=='visible'||Number(style.opacity)===0)throw Error("Protected or unsupported selection");
        }
      }
      protectedNode(range.startContainer);protectedNode(range.endContainer);
      const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT);
      let node;while((node=walk.nextNode())){
        if(++count>4096)throw Error("Document traversal bound reached");
        if(range.intersectsNode(node)){
          protectedNode(node);
          if(node.nodeType===1&&(node.shadowRoot||['IFRAME','FRAME','SCRIPT','STYLE','NOSCRIPT'].includes(node.tagName)))throw Error("Unsupported intersecting surface");
        }
      }
      if(!range.getClientRects().length)throw Error("Visible selection required");
      const text=selection.toString();const bytes=new TextEncoder().encode(text);
      if(!bytes.length||bytes.length>32768||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text))throw Error("Selected text bound or encoding rejected");
      return {text,origin:location.origin,path:location.pathname,url:location.href,captured_at:Math.floor(Date.now()/1000)};
    }});
    const value=results[0];if(!value?.documentId||value.frameId!==0)throw Error("Stable top-frame document identity unavailable");
    const result=value.result;
    if(value.error||!result||Object.keys(result).sort().join()!=="captured_at,origin,path,text,url"||['text','origin','path','url'].some(key=>typeof result[key]!=="string"||!result[key])||!Number.isInteger(result.captured_at)||result.captured_at<=0||new TextEncoder().encode(result.text).length>32768)throw Error("Protected or unsupported selection");
    pendingDraft={...value.result,tab_id:tab.id,document_id:value.documentId,expires_at:Date.now()+300000};
    setTimeout(()=>{pendingDraft=null;},300000);
    await chrome.storage.session.remove(["metadata","receipt","error"]);
    await chrome.windows.create({url:chrome.runtime.getURL("preview.html"),type:"popup",width:720,height:760});
  }catch(e){pendingDraft=null;await chrome.storage.session.set({error:String(e.message??e)});await chrome.windows.create({url:chrome.runtime.getURL("preview.html"),type:"popup",width:720,height:760});}
}
chrome.action.onClicked.addListener(capture);
chrome.contextMenus.onClicked.addListener((info,tab)=>{if(info.menuItemId==="selected-text"&&info.frameId===0)void capture(tab);});
chrome.commands.onCommand.addListener(async command=>{if(command==="review-selected-text"){const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await capture(tab);}});
