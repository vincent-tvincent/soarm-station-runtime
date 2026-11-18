import {BrowserLeaderSerial} from './leader-serial.js';

const clone=value=>structuredClone(value);
const canceled=()=>new DOMException('Leader preparation was canceled','AbortError');
const isCanceled=error=>['AbortError','NotFoundError'].includes(error?.name)||error?.code==='CANCELED';
export function validateBrowserLeaderProfile(profile,arm) {
  if(!profile)throw Error('Select a saved leader profile.');
  if(profile.motor_model!=='sts3215')throw Error('This USB reader supports STS3215 leader motors.');
  const mapping=profile.joint_map,expected=(arm?.joints||[]).map(joint=>joint.name);
  if(!expected.length || !mapping || typeof mapping!=='object' || Array.isArray(mapping) ||
    Object.values(mapping).length!==expected.length || new Set(Object.values(mapping)).size!==expected.length ||
    expected.some(name=>!Object.values(mapping).includes(name)))
    throw Error('The leader profile joint mapping does not match the selected follower.');
  if(typeof profile.gripper_source!=='string' || !profile.gripper_source ||
    Object.hasOwn(mapping,profile.gripper_source) || !profile.calibration || typeof profile.calibration!=='object')
    throw Error('The profile needs a separate calibrated gripper source.');
  return profile;
}
export function validateBrowserLeaderSample(sample,arm) {
  const expected=(arm?.joints||[]).map(joint=>joint.name),positions=sample?.positions;
  if(!positions || typeof positions!=='object' || Array.isArray(positions) ||
    Object.keys(positions).length!==expected.length || expected.some(name=>!Number.isFinite(positions[name])) ||
    !Number.isFinite(sample.gripper) || sample.gripper<0 || sample.gripper>100 || sample.gripper_unit!=='percent')
    throw Error('USB leader returned an incomplete or invalid joint sample.');
  return sample;
}

export class BrowserLeaderPanel {
  constructor({element,manual,serial=new BrowserLeaderSerial(),now=()=>performance.now()}) {
    Object.assign(this,{element,manual,serial,now});
    this.profiles=[];this.selectedId='';this.version=0;this.profileRequest=0;
    this.loading=false;this.pairing=false;this.preparing=false;this.saving=false;this.closePromise=null;
    this.session=null;this.lastSample=null;this.lastSampleAt=0;this.message='Select a profile, then pair a USB leader.';
    element.innerHTML=`<label class="field-label">Leader profile<select id="browser-leader-profile"><option value="">Select a profile</option></select></label>
      <div class="browser-leader-actions"><button type="button" id="browser-leader-pair">Pair USB leader</button><button type="button" id="browser-leader-refresh" class="text-button">Refresh profiles</button><button type="button" id="browser-leader-import-toggle" class="text-button" aria-expanded="false">Import calibration</button></div>
      <p id="browser-leader-status" class="detail-note" role="status"></p>
      <div id="browser-leader-import" class="browser-leader-import" hidden>
        <div class="field-row"><label class="field-label">Profile ID<input id="browser-leader-import-id" placeholder="desk-leader" maxlength="64" /></label><label class="field-label">Name<input id="browser-leader-import-label" placeholder="Desk leader" maxlength="128" /></label></div>
        <label class="field-label">Calibration JSON<input id="browser-leader-calibration" type="file" accept=".json,application/json" /></label>
        <button type="button" id="browser-leader-save">Save profile</button><p class="detail-note">Import an existing leader calibration. This does not recalibrate the device.</p>
      </div>`;
    this.el=id=>element.querySelector('#'+id);
    this.el('browser-leader-pair').onclick=()=>this.pair().catch(error=>this.fail(error));
    this.el('browser-leader-refresh').onclick=()=>this.refreshProfiles().catch(error=>this.fail(error));
    this.el('browser-leader-profile').onchange=()=>{
      const id=this.el('browser-leader-profile').value;
      this.manual.cancelIntent();this.selectedId=id;
      void this.stop('Profile changed. Follow verifies it before moving.').catch(error=>this.fail(error));
    };
    this.el('browser-leader-import-toggle').onclick=()=>{
      const form=this.el('browser-leader-import');form.hidden=!form.hidden;
      this.el('browser-leader-import-toggle').setAttribute('aria-expanded',String(!form.hidden));
    };
    this.el('browser-leader-calibration').onchange=()=>{
      const file=this.el('browser-leader-calibration').files?.[0];if(!file)return;
      if(!this.el('browser-leader-import-id').value)this.el('browser-leader-import-id').value=file.name.replace(/\.json$/i,'').replace(/[^A-Za-z0-9_.-]/g,'-').slice(0,64);
      if(!this.el('browser-leader-import-label').value)this.el('browser-leader-import-label').value=file.name.replace(/\.json$/i,'');
    };
    this.el('browser-leader-save').onclick=()=>this.importProfile().catch(error=>this.fail(error));
    this.render();
  }
  get active(){return Boolean(this.session?.started);}
  get selected(){return this.profiles.find(profile=>profile.id===this.selectedId);}
  get paired(){return Boolean(this.serial.selected??this.portSelected);}
  get armDefinition(){return (this.manual.resources?.arms||[]).find(arm=>arm.id===this.manual.arm)||this.manual.config;}
  profile(){return validateBrowserLeaderProfile(this.selected,this.armDefinition);}
  canFollow() {
    if(!this.serial.supported || !this.paired || this.pairing || this.preparing || this.closePromise || this.saving)return false;
    try{this.profile();return true;}catch{return false;}
  }
  changed(){this.render();this.manual.updateControls();}
  fail(error){
    if(isCanceled(error))return;
    this.message=error.message||String(error);this.render();this.manual.onError(error);
  }
  render() {
    const locked=this.manual.busy||this.active||this.pairing||this.preparing||Boolean(this.closePromise);
    let message=this.message;
    if(!this.serial.supported)message='Web Serial is unavailable. Open this page in desktop Chrome or Edge over HTTPS or localhost. Station input is available for server-attached leaders.';
    else if(this.selected)try{this.profile();}catch(error){message=error.message;}
    this.el('browser-leader-status').textContent=message;
    this.el('browser-leader-profile').disabled=locked||this.loading;
    this.el('browser-leader-pair').disabled=locked||!this.serial.supported||!this.selected;
    this.el('browser-leader-refresh').disabled=locked||this.loading||!this.manual.connected;
    this.el('browser-leader-save').disabled=locked||this.saving||!this.manual.connected||!this.manual.arm;
    this.el('browser-leader-pair').textContent=this.pairing?'Choosing USB…':this.paired?'Choose USB leader':'Pair USB leader';
  }
  renderProfiles() {
    const select=this.el('browser-leader-profile');
    select.replaceChildren(new Option(this.profiles.length?'Select a profile':'No leader profiles saved',''),
      ...this.profiles.map(profile=>new Option(profile.label||profile.id,profile.id)));
    select.value=this.selectedId;this.changed();
  }
  async refreshProfiles() {
    if(!this.manual.connected || this.loading || this.active)return;
    const request=++this.profileRequest,arm=this.manual.arm;this.loading=true;this.render();
    try {
      const response=await this.manual.client.request('/api/leader-profiles');
      if(request!==this.profileRequest || arm!==this.manual.arm || !this.manual.connected)return;
      this.profiles=Array.isArray(response.profiles)?response.profiles:[];
      if(!this.profiles.some(profile=>profile.id===this.selectedId))this.selectedId='';
      if(!this.selectedId) {
        const matching=this.profiles.filter(profile=>{try{validateBrowserLeaderProfile(profile,this.armDefinition);return true;}catch{return false;}});
        if(matching.length===1)this.selectedId=matching[0].id;
      }
      this.renderProfiles();
    } finally {this.loading=false;this.changed();}
  }
  async pair() {
    if(this.active || this.pairing || this.preparing || this.closePromise || this.manual.busy)return false;
    if(!this.serial.supported)throw Error('Web Serial is unavailable; use desktop Chrome or Edge over HTTPS or localhost.');
    const profile=clone(this.profile()),version=++this.version;this.pairing=true;
    // Native chooser MUST be invoked in this click's synchronous call stack.
    let choice;
    try{choice=this.serial.choose(profile);}catch(error){this.pairing=false;this.changed();throw error;}
    this.message='Choose the USB leader in the browser dialog.';this.changed();
    try {
      await choice;if(version!==this.version)return false;
      this.portSelected=true;this.message='USB port selected. Follow verifies calibration before enabling control.';return true;
    } catch(error) {
      if(isCanceled(error)){if(version===this.version)this.message='USB selection canceled.';return false;}
      throw error;
    } finally {this.pairing=false;this.changed();}
  }
  async importProfile() {
    if(this.active || this.preparing || this.saving || this.manual.busy)return;
    const file=this.el('browser-leader-calibration').files?.[0];if(!file)throw Error('Choose a calibration JSON file.');
    const id=this.el('browser-leader-import-id').value.trim(),label=this.el('browser-leader-import-label').value.trim();
    if(!id || !label)throw Error('Enter a profile ID and name.');
    const arm=this.manual.arm;this.saving=true;this.render();
    try {
      const value=JSON.parse(await file.text());
      if(!value || typeof value!=='object' || Array.isArray(value))throw Error('Calibration JSON must be an object.');
      const envelope=value.calibration && typeof value.calibration==='object';
      const body={id,label,arm_id:arm,calibration:envelope?value.calibration:value};
      if(envelope)for(const key of ['motor_model','joint_map','gripper_source','match'])if(Object.hasOwn(value,key))body[key]=value[key];
      if(arm!==this.manual.arm || !this.manual.connected)throw canceled();
      const response=await this.manual.client.request('/api/leader-profiles',body);
      if(arm!==this.manual.arm || !this.manual.connected)return;
      const profile=response.profile||response;validateBrowserLeaderProfile(profile,this.armDefinition);
      this.profiles=this.profiles.filter(item=>item.id!==profile.id).concat(profile);this.selectedId=profile.id;
      this.el('browser-leader-import').hidden=true;
      this.el('browser-leader-import-toggle').setAttribute('aria-expanded','false');
      this.message='Profile saved. Pair a USB leader or Follow with the selected port.';this.renderProfiles();
    } finally {this.saving=false;this.changed();}
  }
  current(session){return this.session===session && session.version===this.version &&
    this.manual.client.connected!==false && this.manual.isBrowserLeader() &&
    this.manual.currentIntent(session.intent) && !session.abort.signal.aborted;}
  async prepare(intent) {
    if(!this.serial.supported)throw Error('Web Serial is unavailable; use a supported desktop browser.');
    if(!this.paired)throw Error('Pair a USB leader first.');
    if(this.preparing||this.pairing||this.active)throw Error('Freeze the current USB leader before following again.');
    const measured=this.manual.measured();if(!measured)throw Error('Fresh follower measurements are required.');
    const profile=clone(this.profile()),session={version:++this.version,intent,profile,abort:new AbortController(),
      started:false,localId:null,lastSent:null,heldGripper:this.manual.preservedGripper(measured)};
    this.session=session;this.preparing=true;this.message='Verifying leader model and calibration…';this.changed();
    try {
      if(this.closePromise)await this.closePromise;
      if(!this.current(session))throw canceled();
      await this.serial.verify(profile);
      if(!this.current(session))throw canceled();
      // Prove a fresh source is readable before permission. Never transmit this preflight sample.
      validateBrowserLeaderSample(await this.serial.sample({signal:session.abort.signal}),this.armDefinition);
      if(!this.current(session))throw canceled();
      this.message='USB calibration verified. Opening the follower connection…';
      return session;
    } catch(error) {
      if(this.session===session)this.session=null;
      await this.closeSerial();
      if(!isCanceled(error))this.message=error.message;
      throw error;
    } finally {this.preparing=false;this.changed();}
  }
  startOptions(session) {
    if(!this.current(session))throw canceled();
    session.started=true;
    return {readLeader:async({signal}={})=>{
      if(!this.current(session)||signal?.aborted)throw canceled();
      const sample=validateBrowserLeaderSample(await this.serial.sample({signal}),this.armDefinition);
      if(!this.current(session)||signal?.aborted)throw canceled();
      this.lastSample=clone(sample);this.lastSampleAt=this.now();
      return {...clone(sample),gripper:this.manual.followGripper?sample.gripper:session.heldGripper};
    },onLeaderSent:body=>{
      if(!this.current(session))return;
      session.lastSent=clone(body);this.manual.target=clone(body);this.manual.reference=clone(body);
      if(this.message!=='Following USB leader.'){this.message='Following USB leader.';this.render();}
    }};
  }
  attachOperation(session,operation) {
    if(operation?.localId)session.localId=operation.localId;
    if(this.current(session)){this.message='Following · waiting for a fresh USB sample.';this.changed();}
  }
  setGripperFollow(value) {
    const session=this.session;
    if(!value && session)session.heldGripper=Number.isFinite(session.lastSent?.gripper)?session.lastSent.gripper:
      this.manual.measured()?this.manual.preservedGripper(this.manual.measured()):session.heldGripper;
  }
  get overlay(){return this.session && this.lastSample && this.now()-this.lastSampleAt<500?this.lastSample:null;}
  closeSerial() {
    if(this.closePromise)return this.closePromise;
    const work=Promise.resolve(this.serial.close());
    this.closePromise=work.finally(()=>{if(this.closePromise===closing)this.closePromise=null;this.changed();});
    const closing=this.closePromise;return closing;
  }
  async stop(message='Frozen. The USB port remains paired.',{retire=true}={}) {
    const session=this.session;this.session=null;++this.version;this.lastSample=null;
    session?.abort.abort();
    let retirement=Promise.resolve();
    if(retire && session?.localId) {
      const operation=this.manual.client.currentOperation;
      retirement=(async()=>{
        this.manual.expectedTermination++;
        try {
          if(operation?.localId===session.localId && operation.operationId && this.manual.client.connected!==false)
            await this.manual.client.hold(session.intent.arm,{controller_session:session.intent.session,
              operator_session_id:session.intent.principal,operation_id:operation.operationId});
        } finally {
          try{await this.manual.client.retireOperation(session.localId);}
          finally{this.manual.expectedTermination--;}
        }
      })();
    }
    const closing=this.closeSerial();this.message=this.paired?message:'Select a profile, then pair a USB leader.';this.changed();
    const outcomes=await Promise.allSettled([retirement,closing]);
    const failed=outcomes.find(outcome=>outcome.status==='rejected');if(failed)throw failed.reason;
  }
  operationEnded(event) {
    if(this.session?.localId && event.localId===this.session.localId)
      return this.stop(event.detail||'Leader run ended. Follow to start again.',{retire:false});
  }
}
