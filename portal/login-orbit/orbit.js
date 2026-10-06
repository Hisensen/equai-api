import { decodedThumb } from './image-cache.js?v=20261005-ui9';
import { createOrbitEntrance, flightPose } from './orbit-entrance.js?v=20261005-ui9';
import { orbitSizing, clearOrbitCore } from './orbit-layout.js?v=20261005-ui9';
export const CDN = new URL('../login-stills/', import.meta.url).href;
export const STILLS = [
  ['hf_20260922_194349_26ffdbfd-ac5e-49e9-a07d-c06d3f7cb4cb','Before the Dust Settled','South Africa · Limpopo Province'],
  ['hf_20260922_194350_5546ea3d-6336-42c7-a59f-06165c5802be','The Long Walk Home','Kenya · Maasai Mara'],
  ['hf_20260922_194349_b4533691-cb49-41d4-b56c-51f0fdcbe250','Something Understood','Botswana · Okavango Delta'],
  ['hf_20260922_194349_e588abd3-1bfa-4918-894f-05632cc51ccc','Nine Hours of Nothing','Finland · Lapland'],
  ['hf_20260922_194350_28d92c80-de66-41cb-911e-b3b44aebe1f5','Borrowed Trust','Scotland · Cairngorms'],
  ['hf_20260922_194349_04e89718-4214-4aff-bac5-490462bbfe2f','Small Weather','Costa Rica · Osa Peninsula'],
  ['hf_20260922_194417_2c031e22-2fad-4c81-a544-83cd6bba1c33','Against the Weather','Alaska · Chilkat Valley'],
  ['hf_20260922_194349_a39c3226-7848-4b15-b840-98ad8aec467b','The Pale Edge','India · Bandhavgarh'],
  ['hf_20260922_194417_555e4d90-f35f-4a1a-8c75-def1e8b71988','Perfect Arithmetic','Indonesia · Raja Ampat'],
  ['hf_20260922_194417_e525a243-03c8-454b-83b4-60f541baf70a','Shallow Water','French Polynesia · Fakarava'],
  ['hf_20260922_194349_ec830e6f-b8e6-4569-8540-ee7f33902c53','Two of Nine','India · Ranthambore'],
  ['hf_20260922_194417_35a9af5f-bd07-45a7-bb73-08b47d19d530','Low Ground','Nepal · Chitwan'],
  ['hf_20260922_194416_30e307a9-1265-45c3-a1a0-5c6fa5bb9f8d','Everything at Once','Iceland · Southern Coast'],
  ['hf_20260922_194417_ff5cb9f8-8eed-4bfb-bb08-11256da92eae','White on White','Canada · Ellesmere Island'],
  ['hf_20260922_194418_1d9bff4a-4971-4944-9e49-d72e755ceeb0','Census','Namibia · Etosha'],
  ['hf_20260922_194349_75e53821-0807-4ebc-992d-34bae0ec2ce6','The Whole Field','France · Provence'],
  ['hf_20260922_194417_5a227847-3796-4438-805d-7e66e9538205','First Season','Germany · Bavarian Forest'],
  ['hf_20260922_194350_b49aa67e-0401-4029-af4f-f6ac3ee83398','Listening Posture','Tanzania · Serengeti'],
  ['hf_20260922_194349_89b82779-3a46-4c55-b7c5-f4a0fd955874','A Line of Red','Spain · Fuente de Piedra'],
  ['hf_20260922_194416_47e18c62-253a-42e1-97a9-9e5f6a6b8d59','Left Behind','Studio · Reykjavík'],
  ['hf_20260922_194417_a455843c-d8db-461c-8ef6-74a325d2472c','The Other Side','Uganda · Kibale Forest'],
];
export const originalThumb = i => `${CDN}${i % STILLS.length}.webp`;
export const thumb = i => decodedThumb(i,originalThumb(i));
const escapeHtml = value => String(value).replace(/[&<>\"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
function renderSceneAccess() {
  return `<div class="scene-access" id="scene-access"><img class="scene-access-logo" src="/equai-mark-white.png" alt="EQUAI" draggable="false"><nav class="scene-buttons" aria-label="登录或注册"><button type="button" class="login-cover-button primary" data-open-auth="login">登录</button><button type="button" class="login-cover-button" data-open-auth="register">注册</button></nav></div>`;
}
export function renderOrbit() {
  return `<div class="orbit-stage" id="orbit-stage" data-entrance="waiting" aria-label="作品图球，可拖动旋转"><div class="orbit-world" id="orbit-world"><div id="orb">${STILLS.map((shot,i)=>`<button class="orbit-card ${i===19?'tall':''}" data-art="${i}" aria-label="参考作品：${escapeHtml(shot[1])}"><figure><img data-shot="${i}" data-original="${originalThumb(i)}" src="${thumb(i)}" alt="${escapeHtml(shot[1])}" decoding="async" draggable="false"></figure></button>`).join('')}</div>${renderSceneAccess()}</div></div>`;
}
export function mountOrbit(openArt) {
  const stage = document.querySelector('#orbit-stage');
  if (!stage) return () => {};
  const world = stage.querySelector('#orbit-world');
  const access = stage.querySelector('#scene-access');
  const cards = [...stage.querySelectorAll('.orbit-card')];
  const gridButton=document.querySelector('.archive-grid-button');
  const bio=document.querySelector('.archive-bio');
  const ga = Math.PI * (3 - Math.sqrt(5));
  const units = cards.map((card,i)=>{
    const y=1-i/(cards.length-1)*2, radial=Math.sqrt(Math.max(0,1-y*y)), theta=i*ga;
    return {card,x:Math.cos(theta)*radial,y,z:Math.sin(theta)*radial};
  });
  const originalUnits = units.map(unit => ({ ...unit }));
  const entrance = createOrbitEntrance(stage, units, { immediate: document.body.classList.contains('archive-grid') });
  let radius=300, yaw=0, pitch=-4, camZ=0, vx=0, vy=0, pointer=null, frame=0, stopped=false;
  const initialPitch= pitch*Math.PI/180;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const render = () => {
    const max=innerHeight*.16;
    if(scrollY>max+1&&document.body.classList.contains('archive-page'))scrollTo(0,max);
    const progress=entrance.active?0:Math.max(0,Math.min(1,scrollY/max));
    const target=progress*Math.min(64,radius*.12);
    camZ=Math.abs(target-camZ)<.01?target:camZ+(target-camZ)*.075;
    const deep=progress>.6,gallery=document.body.classList.contains('archive-grid');
    document.body.classList.toggle('deep',deep);
    if(gridButton){gridButton.tabIndex=deep||gallery?0:-1;gridButton.setAttribute('aria-hidden',String(!deep&&!gallery));}
    if(bio)bio.inert=deep||gallery||entrance.active;
    const transform=`translateZ(${camZ}px) rotateY(${yaw}deg) rotateX(${pitch}deg)`;
    if(world.style.transform!==transform)world.style.transform=transform;
    const center=`rotateX(${-pitch}deg) rotateY(${-yaw}deg) translate(-50%,-50%)`;
    if(access&&access.style.transform!==center)access.style.transform=center;
    const sy=Math.sin(yaw*Math.PI/180), cy=Math.cos(yaw*Math.PI/180), sx=Math.sin(pitch*Math.PI/180), cx=Math.cos(pitch*Math.PI/180);
    const flight = entrance.sample(performance.now());
    units.forEach((unit,index)=>{
      const {card}=unit;
      const pose=flightPose(unit,radius,flight[index]);
      const r=pose.rotation;
      const orientation=`matrix3d(${r[0]},${r[1]},${r[2]},0,${r[3]},${r[4]},${r[5]},0,${r[6]},${r[7]},${r[8]},0,0,0,0,1)`;
      const position=`translate3d(${pose.x}px,${pose.y}px,${pose.z}px) ${orientation} scale(${pose.scale})`;
      if(card.style.transform!==position)card.style.transform=position;
      const visible=String(pose.opacity);
      if(card.style.opacity!==visible)card.style.opacity=visible;
      const depth=((pose.z*cx+pose.y*sx)*cy-pose.x*sy)/radius;
      const base=.14+.86*Math.pow(Math.max(0,Math.min(1,(depth+1)/2)),.85);
      const depthShade=(1-Math.min(1,progress*1.6))*(1-base);
      const finalDepth=unit.z*Math.cos(initialPitch)-unit.y*Math.sin(initialPitch);
      const finalBase=.14+.86*Math.pow(Math.max(0,Math.min(1,(finalDepth+1)/2)),.85);
      // Fade toward the destination's own static brightness so a rear card never
      // brightens mid-flight and darkens again as it returns behind the sphere.
      const shade=String(entrance.active ? 1-finalBase*pose.reveal : depthShade);
      if(card.style.getPropertyValue('--shade')!==shade)card.style.setProperty('--shade',shade);
    });
  };
  const layout=()=>{
    const sizing=orbitSizing(innerWidth,innerHeight);
    radius=sizing.radius;
    stage.style.setProperty('--cw',`${sizing.cardWidth}px`);
    stage.style.setProperty('--persp',`${sizing.perspective}px`);
    stage.style.setProperty('--access-scale',String(sizing.accessScale));
    const rects=[access?.querySelector('.scene-access-logo'),access?.querySelector('.scene-buttons')]
      .filter(Boolean).map(node=>node.getBoundingClientRect());
    if(rects.length&&!document.body.classList.contains('archive-grid')){
      const margin=18*sizing.accessScale;
      const bounds=stage.getBoundingClientRect(),centerX=bounds.left+bounds.width/2,centerY=bounds.top+bounds.height/2;
      const core={left:Math.min(...rects.map(rect=>rect.left))-centerX-margin,
        right:Math.max(...rects.map(rect=>rect.right))-centerX+margin,
        top:Math.min(...rects.map(rect=>rect.top))-centerY-margin,
        bottom:Math.max(...rects.map(rect=>rect.bottom))-centerY+margin};
      originalUnits.forEach((unit,index)=>Object.assign(units[index],clearOrbitCore(unit,sizing,core,{tall:index===19})));
    }
    render();
  };
  const tick=()=>{
    if(stopped)return;
    if(!pointer&&!document.querySelector('#modal')?.open&&(Math.abs(vx)>.002||Math.abs(vy)>.002)){
      yaw+=vx;pitch=Math.max(-32,Math.min(32,pitch+vy));vx*=.94;vy*=.94;
    }
    if(!document.body.classList.contains('archive-grid'))render();
    frame=requestAnimationFrame(tick);
  };
  const down=e=>{
    if(e.target.closest('#scene-access'))return;
    if(entrance.active)return;
    if(e.button && e.button!==0)return;
    pointer={id:e.pointerId,startX:e.clientX,startY:e.clientY,x:e.clientX,y:e.clientY,art:e.target.closest('[data-art]')?.dataset.art,touch:e.pointerType==='touch',captured:false,moved:false};
    vx=vy=0;
    if(!pointer.touch){stage.setPointerCapture(e.pointerId);pointer.captured=true;stage.classList.add('dragging');}
  };
  const move=e=>{
    if(!pointer || e.pointerId!==pointer.id)return;
    const dx=e.clientX-pointer.startX,dy=e.clientY-pointer.startY;
    if(pointer.touch&&!pointer.captured){
      if(Math.hypot(dx,dy)<10)return;
      if(Math.abs(dy)>Math.abs(dx)*1.15){pointer=null;return;}
      stage.setPointerCapture(e.pointerId);pointer.captured=true;stage.classList.add('dragging');
    }
    if(Math.hypot(dx,dy)>(pointer.touch?14:6))pointer.moved=true;
    const mx=e.clientX-pointer.x,my=e.clientY-pointer.y;
    yaw+=mx*.13;pitch=Math.max(-32,Math.min(32,pitch-my*.13));
    vx=reduced?0:mx*.12;vy=reduced?0:-my*.12;
    pointer.x=e.clientX;pointer.y=e.clientY;render();
  };
  const up=e=>{
    if(!pointer||e.pointerId!==pointer.id)return;
    const p=pointer;pointer=null;stage.classList.remove('dragging');
    if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);
    if(!p.moved && p.art!==undefined)openArt(Number(p.art));
  };
  const preventNativeDrag=event=>event.preventDefault();
  const cancel=()=>{if(pointer&&stage.hasPointerCapture(pointer.id))stage.releasePointerCapture(pointer.id);pointer=null;stage.classList.remove('dragging');};
  const key=e=>{if(!entrance.active&&(e.key==='Enter'||e.key===' ')){const card=e.target.closest('[data-art]');if(card){e.preventDefault();openArt(Number(card.dataset.art));}}};
  stage.addEventListener('dragstart',preventNativeDrag);stage.addEventListener('selectstart',preventNativeDrag);
  stage.addEventListener('pointerdown',down);stage.addEventListener('pointermove',move);stage.addEventListener('pointerup',up);stage.addEventListener('pointercancel',cancel);stage.addEventListener('keydown',key);
  window.addEventListener('resize',layout);layout();tick();
  return ()=>{
    stopped=true;cancel();cancelAnimationFrame(frame);entrance.dispose();window.removeEventListener('resize',layout);
    stage.removeEventListener('dragstart',preventNativeDrag);stage.removeEventListener('selectstart',preventNativeDrag);
    stage.removeEventListener('pointerdown',down);stage.removeEventListener('pointermove',move);stage.removeEventListener('pointerup',up);stage.removeEventListener('pointercancel',cancel);stage.removeEventListener('keydown',key);
  };
}
