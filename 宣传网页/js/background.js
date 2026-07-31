import * as THREE from 'three';
import { EffectComposer, RenderPass, EffectPass, BloomEffect, ChromaticAberrationEffect } from 'postprocessing';

const frag = `precision highp float;
uniform vec3 iResolution;uniform float iTime;uniform vec2 uSkew;uniform float uTilt;uniform float uYaw;
uniform float uLineThickness;uniform vec3 uLinesColor;uniform vec3 uScanColor;uniform float uGridScale;
uniform float uLineStyle;uniform float uLineJitter;uniform float uScanOpacity;uniform float uScanDirection;
uniform float uNoise;uniform float uBloomOpacity;uniform float uScanGlow;uniform float uScanSoftness;
uniform float uPhaseTaper;uniform float uScanDuration;uniform float uScanDelay;
varying vec2 vUv;
uniform float uScanStarts[8];uniform float uScanCount;
const int MAX_SCANS=8;
float sm01(float a,float b,float x){float t=clamp((x-a)/max(1e-5,(b-a)),0.,1.);return t*t*t*(t*(t*6.-15.)+10.);}
void mainImage(out vec4 c,in vec2 fc){
vec2 p=(2.*fc-iResolution.xy)/iResolution.y;
vec3 ro=vec3(0.);vec3 rd=normalize(vec3(p,2.));
float cR=cos(uTilt),sR=sin(uTilt);rd.xy=mat2(cR,-sR,sR,cR)*rd.xy;
float cY=cos(uYaw),sY=sin(uYaw);rd.xz=mat2(cY,-sY,sY,cY)*rd.xz;
vec2 sk=clamp(uSkew,vec2(-.7),vec2(.7));rd.xy+=sk*rd.z;
vec3 col=vec3(0.);float minT=1e20;float gs=max(1e-5,uGridScale);float fadeStr=2.;vec2 gv=vec2(0.);float hiy=1.;
for(int i=0;i<4;i++){float iy=float(i<2);float pos=mix(-.2,.2,float(i))*iy+mix(-.5,.5,float(i-2))*(1.-iy);
float num=pos-(iy*ro.y+(1.-iy)*ro.x);float den=iy*rd.y+(1.-iy)*rd.x;float t=num/den;vec3 h=ro+rd*t;
float db=smoothstep(0.,3.,h.z);h.xy+=sk*0.15*db;bool use=t>0.&&t<minT;gv=use?mix(h.zy,h.xz,iy)/gs:gv;minT=use?t:minT;hiy=use?iy:hiy;}
vec3 hit=ro+rd*minT;float dist=length(hit-ro);
float jA=clamp(uLineJitter,0.,1.);
if(jA>0.){vec2 j=vec2(sin(gv.y*2.7+iTime*1.8),cos(gv.x*2.3-iTime*1.6))*0.15*jA;gv+=j;}
float fx=fract(gv.x),fy=fract(gv.y);float ax=min(fx,1.-fx),ay=min(fy,1.-fy);
float wx=fwidth(gv.x),wy=fwidth(gv.y);float hp=max(0.,uLineThickness)*.5;
float lx=1.-smoothstep(hp*wx,hp*wx+wx,ax);float ly=1.-smoothstep(hp*wy,hp*wy+wy,ay);
float pm=max(lx,ly);
vec2 gv2=(hiy>.5?hit.xz:hit.zy)/gs;
if(jA>0.){vec2 j2=vec2(cos(gv2.y*2.1-iTime*1.4),sin(gv2.x*2.5+iTime*1.7))*0.15*jA;gv2+=j2;}
float fx2=fract(gv2.x),fy2=fract(gv2.y);float ax2=min(fx2,1.-fx2),ay2=min(fy2,1.-fy2);
float wx2=fwidth(gv2.x),wy2=fwidth(gv2.y);
float lx2=1.-smoothstep(hp*wx2,hp*wx2+wx2,ax2);float ly2=1.-smoothstep(hp*wy2,hp*wy2+wy2,ay2);
float am=max(lx2,ly2);
float edx=min(abs(hit.x-(-.5)),abs(hit.x-.5));float edy=min(abs(hit.y-(-.2)),abs(hit.y-.2));
float ed=mix(edy,edx,hiy);float eg=1.-smoothstep(gs*.5,gs*2.,ed);am*=eg;
float lm=max(pm,am);float fde=exp(-dist*fadeStr);
float dur=max(.05,uScanDuration);float del=max(0.,uScanDelay);float szm=2.;
float ws=max(.1,uScanGlow);float sg=max(.001,.18*ws*uScanSoftness);float sga=sg*2.;
float cp=0.,ca=0.;float cyc=dur+del;float tc=mod(iTime,cyc);float sp=clamp((tc-del)/dur,0.,1.);float ph=sp;
if(uScanDirection>.5&&uScanDirection<1.5)ph=1.-ph;else if(uScanDirection>1.5){float t2=mod(max(0.,iTime-del),2.*dur);ph=(t2<dur)?(t2/dur):(1.-(t2-dur)/dur);}
float sz=ph*szm;float dz=abs(hit.z-sz);float lb=exp(-.5*(dz*dz)/(sg*sg));
float tp=clamp(uPhaseTaper,0.,.49);float hw=tp;float tw=tp;
float hf=sm01(0.,hw,ph);float tf=1.-sm01(1.-tw,1.,ph);float pw=hf*tf;
cp+=lb*pw*clamp(uScanOpacity,0.,1.);float ab=exp(-.5*(dz*dz)/(sga*sga));ca+=(ab*.25)*pw*clamp(uScanOpacity,0.,1.);
for(int i=0;i<MAX_SCANS;i++){if(float(i)>=uScanCount)break;float ta=iTime-uScanStarts[i];float pi=clamp(ta/dur,0.,1.);
if(uScanDirection>.5&&uScanDirection<1.5)pi=1.-pi;else if(uScanDirection>1.5)pi=(pi<.5)?(pi*2.):(1.-(pi-.5)*2.);
float szi=pi*szm;float dzi=abs(hit.z-szi);float lbi=exp(-.5*(dzi*dzi)/(sg*sg));
float hfi=sm01(0.,hw,pi);float tfi=1.-sm01(1.-tw,1.,pi);float pwi=hfi*tfi;
cp+=lbi*pwi*clamp(uScanOpacity,0.,1.);float abi=exp(-.5*(dzi*dzi)/(sga*sga));ca+=(abi*.25)*pwi*clamp(uScanOpacity,0.,1.);}
float lv=lm;vec3 gc=uLinesColor*lv*fde;vec3 sc=uScanColor*cp;vec3 sa=uScanColor*ca;
col=gc+sc+sa;
float n=fract(sin(dot(gl_FragCoord.xy+vec2(iTime*123.4),vec2(12.9898,78.233)))*43758.5453123);
col+=(n-.5)*uNoise;col=clamp(col,0.,1.);
float al=clamp(max(lv,cp),0.,1.);
float gx=1.-smoothstep(hp*wx*2.,hp*wx*2.+wx*2.,ax);float gy=1.-smoothstep(hp*wy*2.,hp*wy*2.+wy*2.,ay);
float halo=max(gx,gy)*fde;al=max(al,halo*clamp(uBloomOpacity,0.,1.));
c=vec4(col,al);}
void main(){vec4 c;mainImage(c,vUv*iResolution.xy);gl_FragColor=c;}`;

const vert = `varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`;

function srgbColor(hex){const c=new THREE.Color(hex);return c.convertSRGBToLinear();}

const container=document.getElementById('bg-canvas');
if(container){
container.style.position='fixed';container.style.top='0';container.style.left='0';
container.style.width='100vw';container.style.height='100vh';container.style.zIndex='0';

const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});
renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
renderer.setSize(window.innerWidth,window.innerHeight);
renderer.outputColorSpace=THREE.SRGBColorSpace;
renderer.toneMapping=THREE.NoToneMapping;
renderer.autoClear=false;
renderer.setClearColor(0x000000,0);
container.appendChild(renderer.domElement);

const uniforms={
  iResolution:{value:new THREE.Vector3(window.innerWidth,window.innerHeight,renderer.getPixelRatio())},
  iTime:{value:0},uSkew:{value:new THREE.Vector2(0,0)},
  uTilt:{value:0},uYaw:{value:0},
  uLineThickness:{value:1},
  uLinesColor:{value:srgbColor('#2F293A')},
  uScanColor:{value:srgbColor('#FF9FFC')},
  uGridScale:{value:0.1},
  uLineStyle:{value:0},
  uLineJitter:{value:0.1},
  uScanOpacity:{value:0.4},
  uNoise:{value:0.01},
  uBloomOpacity:{value:0.6},
  uScanGlow:{value:0.5},
  uScanSoftness:{value:2},
  uPhaseTaper:{value:0.9},
  uScanDirection:{value:2},
  uScanDuration:{value:2},
  uScanDelay:{value:2},
  uScanStarts:{value:new Array(8).fill(0)},
  uScanCount:{value:0},
};

const material=new THREE.ShaderMaterial({
  uniforms,vertexShader:vert,fragmentShader:frag,
  transparent:true,depthWrite:false,depthTest:false
});
const scene2=new THREE.Scene();
const camera2=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
const quad=new THREE.Mesh(new THREE.PlaneGeometry(2,2),material);
scene2.add(quad);

let composer=null;
try{
  composer=new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene2,camera2));
  const bloom=new BloomEffect({intensity:1,luminanceThreshold:0,luminanceSmoothing:0});
  bloom.blendMode.opacity.value=0.6;
  const chroma=new ChromaticAberrationEffect({
    offset:new THREE.Vector2(0.002,0.002),
    radialModulation:true,modulationOffset:0
  });
  const ep=new EffectPass(camera2,bloom,chroma);
  ep.renderToScreen=true;
  composer.addPass(ep);
}catch(e){console.warn('Post-processing not available:',e);composer=null;}

const lookTarget=new THREE.Vector2(0,0);
const lookCurrent=new THREE.Vector2(0,0);
const lookVel=new THREE.Vector2(0,0);
const tiltTarget={v:0};const tiltCurrent={v:0};const tiltVel={v:0};
const yawTarget={v:0};const yawCurrent={v:0};const yawVel={v:0};

const smoothTime=0.28;const maxSpeed=Infinity;
const skewScale=0.13;const tiltScale=0.21;const yawScale=0.19;const yBoost=1.4;

function smoothDampFloat(cur,tar,velRef,sTime,mSpeed,dTime){
  sTime=Math.max(0.0001,sTime);
  const omega=2/sTime;const x=omega*dTime;
  const exp=1/(1+x+0.48*x*x+0.235*x*x*x);
  let change=cur-tar;const origTar=tar;
  const maxChg=mSpeed*sTime;change=Math.sign(change)*Math.min(Math.abs(change),maxChg);
  tar=cur-change;const temp=(velRef.v+omega*change)*dTime;
  velRef.v=(velRef.v-omega*temp)*exp;
  let out=tar+(change+temp)*exp;
  if((origTar-cur)*(out-origTar)>0){out=origTar;velRef.v=0;}
  return {value:out,v:velRef.v};
}

function smoothDampVec2(cur,tar,cv,sTime,mSpeed,dTime){
  const out=cur.clone();sTime=Math.max(0.0001,sTime);
  const omega=2/sTime;const x=omega*dTime;
  const exp=1/(1+x+0.48*x*x+0.235*x*x*x);
  let change=cur.clone().sub(tar);const origTar=tar.clone();
  const maxChg=mSpeed*sTime;if(change.length()>maxChg)change.setLength(maxChg);
  tar=cur.clone().sub(change);const temp=cv.clone().addScaledVector(change,omega).multiplyScalar(dTime);
  cv.sub(temp.clone().multiplyScalar(omega));cv.multiplyScalar(exp);
  out.copy(tar.clone().add(change.add(temp).multiplyScalar(exp)));
  if(origTar.clone().sub(cur).dot(out.clone().sub(origTar))>0){out.copy(origTar);cv.set(0,0);}
  return out;
}

let mouseLeaveTimer=null;
document.addEventListener('mousemove',e=>{
  const rect=container.getBoundingClientRect();
  const nx=((e.clientX-rect.left)/rect.width)*2-1;
  const ny=-(((e.clientY-rect.top)/rect.height)*2-1);
  lookTarget.set(nx,ny);
  if(mouseLeaveTimer){clearTimeout(mouseLeaveTimer);mouseLeaveTimer=null;}
});
document.addEventListener('mouseleave',()=>{
  lookTarget.set(0,0);tiltTarget.v=0;yawTarget.v=0;
});

window.addEventListener('resize',()=>{
  const w=window.innerWidth,h=window.innerHeight;
  renderer.setSize(w,h);
  material.uniforms.iResolution.value.set(w,h,renderer.getPixelRatio());
  if(composer)composer.setSize(w,h);
});

let last=performance.now();
function animate(){
  requestAnimationFrame(animate);
  const now=performance.now();const dt=Math.max(0,Math.min(0.1,(now-last)/1000));last=now;

  lookCurrent.copy(smoothDampVec2(lookCurrent,lookTarget,lookVel,smoothTime,maxSpeed,dt));
  const ts=smoothDampFloat(tiltCurrent.v,tiltTarget.v,tiltVel,smoothTime,maxSpeed,dt);
  tiltCurrent.v=ts.value;tiltVel.v=ts.v;
  const ys=smoothDampFloat(yawCurrent.v,yawTarget.v,yawVel,smoothTime,maxSpeed,dt);
  yawCurrent.v=ys.value;yawVel.v=ys.v;

  const skew=new THREE.Vector2(lookCurrent.x*skewScale,-lookCurrent.y*yBoost*skewScale);
  material.uniforms.uSkew.value.set(skew.x,skew.y);
  material.uniforms.uTilt.value=tiltCurrent.v*tiltScale;
  material.uniforms.uYaw.value=THREE.MathUtils.clamp(yawCurrent.v*yawScale,-0.6,0.6);
  material.uniforms.iTime.value=now/1000;

  renderer.clear(true,true,true);
  if(composer){composer.render(dt);}else{renderer.render(scene2,camera2);}
}
animate();
}