const button=document.getElementById('enable'),status=document.getElementById('status');
async function refresh(){
 const permission=await navigator.permissions.query({name:'microphone'});
 const update=()=>{button.hidden=permission.state==='granted';status.textContent=permission.state==='granted'?'Microphone enabled. You can close this page.':'Allow microphone access to enable background audio.';};
 permission.onchange=update;update();
}
button.onclick=async()=>{
 button.disabled=true;
 try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});stream.getTracks().forEach(t=>t.stop());await refresh();}
 catch{status.textContent='Microphone permission was not granted. Allow access in Chrome, then try again.';}
 finally{button.disabled=false;}
};
void refresh();
