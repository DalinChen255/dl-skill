import test from "node:test";
import assert from "node:assert/strict";
import {makeReviewPacket, automaticDecisions, approvePacket, reviewedHistoryCache} from "./xhs_business_review.mjs";
const msg=(id,text,side="left",extra={})=>({id, text, side, type:"TEXT", timestamp:Number(id.slice(1))*1000,...extra});
const obs=(messages,extra={})=>({status:"observed",uid:"u1",search_complete:true,coverage_complete:true,
  discovered_account_ids:["a1"],reviews:[{user_id:"u1",account_id:"a1",identity_verified:true,panel_loaded:true,
    history_complete:true,history_from_start_complete:true,label:"none",history_boundary_message_id:messages.at(-1).id,
    messages,outbound_text_hashes:[],...extra}]});
const approve=(p,d={})=>approvePacket(p,{evidence_id:p.evidence_id,decisions:[{account_id:"a1",contact_shared:false,stop_contact:false,evidence:"",...d}]});

test("complete greeting and course questions are automatic; fragments and ambiguous confirmations are not",()=>{
  for(const text of ["你好","在哪举办？","了解线下课","请问是免费参加吗？"])
    assert.equal(automaticDecisions(makeReviewPacket(obs([msg("m1",text)]))).length,1);
  for(const text of ["好","是","OK","22","777","我加你吧","已经加了","不需要","138","Ad后面am的00后070"])
    assert.equal(automaticDecisions(makeReviewPacket(obs([msg("m1",text)]))),null,text);
});
test("one difficult account prevents a global automatic clearance",()=>{
  const o=obs([msg("m1","你好")]);o.discovered_account_ids.push("a2");
  o.reviews.push({...o.reviews[0],account_id:"a2",messages:[msg("m2","1380013")],history_boundary_message_id:"m2"});
  assert.equal(automaticDecisions(makeReviewPacket(o)),null);
});
test("media, shared cards, missing identity and incomplete history cannot enter the fast path",()=>{
  for(const extra of [{type:"CARD"},{type:"IMAGE"},{type:"GENERAL",plainText:false}])
    assert.equal(automaticDecisions(makeReviewPacket(obs([msg("m1","你好","left",extra)]))),null);
  assert.equal(makeReviewPacket(obs([msg("m1","你好")],{identity_verified:false})),null);
  assert.equal(automaticDecisions(makeReviewPacket(obs([msg("m1","你好")],{history_from_start_complete:false}))),null);
});
test("approved history can be reused after time passes; newest live identity remains external",()=>{
  const p=approve(makeReviewPacket(obs([msg("m1","我们做建材")])));
  const o=obs([msg("m1","我们做建材"),msg("m2","发您资料","right")],{history_from_start_complete:false,history_reached_prior_boundary:true});
  const fresh=makeReviewPacket(o,p);
  assert.equal(fresh.reviews[0].context_complete,true);
  assert.equal(automaticDecisions(fresh,p).length,1);
  assert.equal(reviewedHistoryCache(p).a1.boundary_message_id,"m1");
  assert.equal("token" in p,false);
});
test("split phone context is preserved across an incremental boundary and requires review",()=>{
  const p=approve(makeReviewPacket(obs([msg("m1","138"),msg("m2","您好","right")])));
  const next=makeReviewPacket(obs([msg("m2","您好","right"),msg("m3","00138000")],
    {history_from_start_complete:false,history_reached_prior_boundary:true}),p);
  assert.equal(next.reviews[0].messages[0].text,"138");
  assert.equal(automaticDecisions(next,p),null);
});
test("an unproved or altered boundary never imports older evidence",()=>{
  const p=approve(makeReviewPacket(obs([msg("m1","我们做建材")])));
  for(const messages of [[msg("m2","你好")],[msg("m1","修改内容")]]) {
    const next=makeReviewPacket(obs(messages,{history_from_start_complete:false,history_reached_prior_boundary:true}),p);
    assert.equal(next.reviews[0].context_complete,false);
    assert.equal(automaticDecisions(next,p),null);
  }
});
test("approval requires matching evidence, all accounts, and a real customer opt-out quote",()=>{
  const p=makeReviewPacket(obs([msg("m1","不要再联系我")]));
  assert.throws(()=>approvePacket(p,{evidence_id:"stale",decisions:[]}),/evidence_changed/);
  assert.throws(()=>approve(p,{stop_contact:true,evidence:"别再发"}),/customer_quote/);
  assert.equal(approve(p,{stop_contact:true,evidence:"不要再联系我"}).approved,true);
  const image=makeReviewPacket(obs([msg("m1","图片","left",{type:"IMAGE"})],{needs_visual_review:true}));
  assert.throws(()=>approve(image),/visual_review_required/);
});
test("changed old text and newly discovered accounts require their own review",()=>{
  const p=approve(makeReviewPacket(obs([msg("m1","建材")])));
  assert.equal(automaticDecisions(makeReviewPacket(obs([msg("m1","不要再发")]),p),p),null);
  const o=obs([msg("m1","建材")]);o.discovered_account_ids.push("a2");
  o.reviews.push({...o.reviews[0],account_id:"a2",messages:[msg("m2","加了")],history_boundary_message_id:"m2"});
  assert.equal(automaticDecisions(makeReviewPacket(o,p),p),null);
});

test("a verified unchanged visual message can reuse its decision, but a new image cannot",()=>{
  const o=obs([msg("m1","图片","left",{type:"IMAGE"})],{needs_visual_review:true});
  const packet=makeReviewPacket(o);
  const approved=approvePacket(packet,{evidence_id:packet.evidence_id,visual_reviewed:true,
    decisions:[{account_id:"a1",contact_shared:false,stop_contact:false}]});
  assert.equal(automaticDecisions(makeReviewPacket(o,approved),approved).length,1);
  const changed=obs([...o.reviews[0].messages,msg("m2","新图片","left",{type:"IMAGE"})],{needs_visual_review:true});
  assert.equal(automaticDecisions(makeReviewPacket(changed,approved),approved),null);
});

// These fixtures exercise real packet creation and approval. A zero-addition
// visual gate wrongly rejects new plaintext; a type-blind gate admits new media.
const visuallyApprovedHistory=()=>{
  const packet=makeReviewPacket(obs([
    msg("m1","图片","left",{type:"IMAGE"}),msg("m2","您好","right"),
  ],{needs_visual_review:true}));
  return approvePacket(packet,{evidence_id:packet.evidence_id,visual_reviewed:true,
    decisions:[{account_id:"a1",contact_shared:false,stop_contact:false,evidence:""}]});
};

test("own new plaintext preserves approval of unchanged historical images",()=>{
  const approved=visuallyApprovedHistory();
  for(const extra of [{type:"TEXT"},{type:"GENERAL",plainText:true}]) {
    const next=makeReviewPacket(obs([...approved.reviews[0].messages,
      msg("m3","发您课程资料","right",extra)],{needs_visual_review:true}),approved);
    assert.deepEqual(automaticDecisions(next,approved),[
      {account_id:"a1",contact_shared:false,stop_contact:false,evidence:""},
    ]);
  }
});

test("new complete whitelisted customer questions preserve historical visual approval",()=>{
  const approved=visuallyApprovedHistory();
  const next=makeReviewPacket(obs([...approved.reviews[0].messages,
    msg("m3","在哪举办？")],{needs_visual_review:true}),approved);
  assert.deepEqual(automaticDecisions(next,approved),[
    {account_id:"a1",contact_shared:false,stop_contact:false,evidence:""},
  ]);
});

test("a known platform text hint preserves approval of unchanged historical images",()=>{
  const approved=visuallyApprovedHistory();
  const next=makeReviewPacket(obs([...approved.reviews[0].messages,
    msg("m3","对方关注或回复你之前，24小时内最多只能发1条文字消息","center",{type:"HINT"})],
  {needs_visual_review:true}),approved);
  assert.deepEqual(automaticDecisions(next,approved),[
    {account_id:"a1",contact_shared:false,stop_contact:false,evidence:""},
  ]);
});

test("new media or unresolved plaintext never inherits historical visual approval",()=>{
  const approved=visuallyApprovedHistory();
  for(const side of ["left","right","center"]) {
    for(const extra of [
      {type:"IMAGE"},{type:"PICTURE"},{type:"VIDEO"},{type:"FILE"},{type:"CARD"},{type:"RICH_HINT"},
      {type:"UNKNOWN"},{type:"GENERAL",plainText:false},
      {type:"TEXT",text:"[图片]"},{type:"TEXT",text:""},
    ]) {
      const next=makeReviewPacket(obs([...approved.reviews[0].messages,
        msg("m3","新内容",side,extra)],{needs_visual_review:true}),approved);
      assert.equal(automaticDecisions(next,approved),null,`${side} ${extra.type} ${extra.text ?? ""}`);
    }
  }
  const customerHint=makeReviewPacket(obs([...approved.reviews[0].messages,
    msg("m3","你好","left",{type:"HINT"})],{needs_visual_review:true}),approved);
  assert.equal(automaticDecisions(customerHint,approved),null);
});

test("new outbound media requires review even without a customer visual flag",()=>{
  const approved=approve(makeReviewPacket(obs([msg("m1","你好")])));
  const next=makeReviewPacket(obs([msg("m1","你好"),
    msg("m2","图片","right",{type:"IMAGE"})],{needs_visual_review:false}),approved);
  assert.equal(automaticDecisions(next,approved),null);
});

test("an incremental boundary retains the visually approved older history",()=>{
  const approved=visuallyApprovedHistory();
  const next=makeReviewPacket(obs([approved.reviews[0].messages[1],
    msg("m3","发您课程资料","right")],
  {history_from_start_complete:false,history_reached_prior_boundary:true}),approved);
  assert.equal(next.reviews[0].messages[0].type,"IMAGE");
  assert.deepEqual(automaticDecisions(next,approved),[
    {account_id:"a1",contact_shared:false,stop_contact:false,evidence:""},
  ]);
});

test("new ambiguous customer text still requires semantic review after visual approval",()=>{
  const approved=visuallyApprovedHistory();
  for(const text of ["13800138000","微信","已经加了","不需要","好","22","课程多少钱？"]) {
    const next=makeReviewPacket(obs([...approved.reviews[0].messages,
      msg("m3",text)],{needs_visual_review:true}),approved);
    assert.equal(automaticDecisions(next,approved),null,text);
  }
});

test("changed historical visual evidence requires a new review",()=>{
  const approved=visuallyApprovedHistory();
  for(const change of [{text:"另一张图片"},{type:"VIDEO"},{side:"right"},{timestamp:1500}]) {
    const next=makeReviewPacket(obs([
      {...approved.reviews[0].messages[0],...change},approved.reviews[0].messages[1],
      msg("m3","发您课程资料","right"),
    ],{needs_visual_review:true}),approved);
    assert.equal(automaticDecisions(next,approved),null,JSON.stringify(change));
  }
});

test("a full current history that lost an approved image cannot reconstruct its visual approval",()=>{
  const approved=visuallyApprovedHistory();
  const next=makeReviewPacket(obs([
    approved.reviews[0].messages[1],msg("m3","发您课程资料","right"),
  ],{history_reached_prior_boundary:true}),approved);
  assert.equal(automaticDecisions(next,approved),null);
});

test("a new account cannot borrow another account's historical visual approval",()=>{
  const approved=visuallyApprovedHistory();
  const nextObservation=obs([...approved.reviews[0].messages,
    msg("m3","发您课程资料","right")],{needs_visual_review:true});
  nextObservation.discovered_account_ids.push("a2");
  nextObservation.reviews.push({...nextObservation.reviews[0],account_id:"a2",
    messages:[msg("m4","图片","left",{type:"IMAGE"})],history_boundary_message_id:"m4"});
  assert.equal(automaticDecisions(makeReviewPacket(nextObservation,approved),approved),null);
});

test("a DOM-proved own decorative thumbnail reuses unchanged visual history",()=>{
  const approved=visuallyApprovedHistory();
  const next=makeReviewPacket(obs([...approved.reviews[0].messages,
    msg("m3","发您课程资料","right",{type:"GENERAL",plainText:false,
      extracted_text_complete:true,decorative_media_only:true})],{needs_visual_review:true}),approved);
  assert.deepEqual(automaticDecisions(next,approved),[
    {account_id:"a1",contact_shared:false,stop_contact:false,evidence:""},
  ]);
});

test("decorative flags cannot exempt customer media or incomplete own text",()=>{
  const approved=visuallyApprovedHistory();
  for(const extra of [
    {side:"left",type:"GENERAL",extracted_text_complete:true,decorative_media_only:true},
    {side:"right",type:"GENERAL",extracted_text_complete:false,decorative_media_only:true},
    {side:"right",type:"GENERAL",extracted_text_complete:true,decorative_media_only:false},
    {side:"right",type:"IMAGE",extracted_text_complete:true,decorative_media_only:true},
  ]) {
    const next=makeReviewPacket(obs([...approved.reviews[0].messages,
      msg("m3","新图片","right",extra)],{needs_visual_review:true}),approved);
    assert.equal(automaticDecisions(next,approved),null);
  }
});

test("changing media proof invalidates the previous approval fingerprint",()=>{
  const message=msg("m1","课程资料","right",{type:"GENERAL",plainText:false,
    extracted_text_complete:true,decorative_media_only:true});
  const first=makeReviewPacket(obs([message]));
  for(const change of [{extracted_text_complete:false},{decorative_media_only:false}]) {
    const next=makeReviewPacket(obs([{...message,...change}]));
    assert.notEqual(next.evidence_id,first.evidence_id);
    assert.throws(()=>approvePacket(next,{evidence_id:first.evidence_id,decisions:[]}),/evidence_changed/);
  }
});
