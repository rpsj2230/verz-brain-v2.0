module.exports = [
{id:"M36",name:"Scaling triggers and performance",wave:8,tasks:[
 {n:"Deferred scaling work, each behind a written trigger",s:[
   {n:"Ledger partitioning",k:["pg_partman on the metadata ledger","Partition by month with automatic creation","Retention detach and archive rather than delete","Trigger: ledger row count or query latency threshold"]},
   {n:"Read replica",k:["Streaming replica configuration","Console reads routed to the replica","Replication lag monitoring with a staleness banner","Trigger: console queries affecting answer latency"]},
   {n:"Materialised reporting views",k:["Views for the heaviest report screens","Refresh schedule and staleness display","Trigger: a report exceeding two seconds"]},
   {n:"Two-box split",k:["Application and browser on one host, database and object store on the other","Rehearsed without an application change","Trigger: RAM pressure or a resilience requirement"]}
 ]},
 {n:"Performance work, sized only after measurement",s:[
   {n:"Parallel connector execution",k:["Dependency graph over independent source calls","Fan-out with one critical path rather than a sum","Per-source and global budgets still enforced under parallelism"]},
   {n:"Guard checkpoint collapse",k:["Measure the serial cost of the three screens","Collapse into a single pass where semantics allow","Verify no detection coverage is lost"]},
   {n:"Request coalescing",k:["Identical in-flight requests share one execution","Entitlement equality required before sharing, never similarity"]},
   {n:"Load test",k:["Peak concurrency test rather than daily volume","Documented first bottleneck at ten and one hundred times","Results recorded against the SLO table"]}
 ]}
]},
{id:"M37",name:"Migration, launch and handover",wave:9,tasks:[
 {n:"Migration from the v1 Company Brain",s:[
   {n:"Inventory",k:["Catalogue what the current Company Brain holds: knowledge, prompts, tool definitions, conversation history","Decide per item: migrate, rebuild or retire","Map old permissions onto the new capability model, explicitly rather than by assumption"]},
   {n:"Data movement",k:["Knowledge and asset export from the old system","Re-parse and re-embed rather than copying vectors, since embeddings are model-specific","Entity registry seeded from existing client records","Historical conversations archived, not imported, unless there is a stated reason"]},
   {n:"Parallel running",k:["Both systems answering the same questions for an agreed period","Answer comparison log with disagreements surfaced","Cutover criteria agreed in writing before the period starts"]},
   {n:"Decommission",k:["Old system read-only, then archived","Credential revocation on every connector the old system held","Scheduled jobs disabled and verified stopped","Final backup retained per the retention policy"]}
 ]},
 {n:"Replacing AnyGen",s:[
   {n:"What is actually in there",k:["Inventory the twelve house skills, named individually, since each is authored work and not a setting","Inventory every agent with its connectors, skills, channels and availability, because an agent is a composition and not a prompt","Inventory automations, which are owned by an agent there and will be owned by an agent here","Export memory files, both curated and chat-extracted, with their change history","List which Lark groups each agent is installed into, since that is what staff actually see","Record what AnyGen produced that anyone still relies on"]},
   {n:"Skills come across, they are not rewritten",k:["Export verz-master-theme, verz-doc-letterhead, seo-audit and website-cro-audit first: they are in daily use and are the test of whether import works at all","Convert the description convention, since theirs are written as router prompts beginning \"Use the X skill to\"","Re-review every imported skill through the normal queue rather than trusting it because it worked before","Diff behaviour on a real task before and after import, not just that the file parses","Retire any skill that has not been invoked in ninety days rather than carrying it over"]},
   {n:"Agents are rebuilt, deliberately",k:["Rebuild each agent from a template rather than importing its configuration, because their model has no ceiling and no leash to carry across","Set an explicit ceiling per agent, which AnyGen never had","Start every rebuilt agent at Shadow on writes regardless of how it behaved there","Map their Availability onto our availability, and their absent authority onto our entitlement, separately - conflating the two is the mistake their model makes"]},
   {n:"Adaptive learning does not transfer",k:["Their learning is one toggle over an opaque store; ours is four tiers with a review queue, and there is no honest mapping between them","Read the memory files as evidence and re-derive tier one preferences rather than importing beliefs wholesale","Discard anything that would widen a scope, since it was never approved by anyone under our rules","State plainly in the handover that the new system starts without their accumulated learning, and why that is the right trade"]},
   {n:"Cutover in Lark, where staff will notice",k:["Install alongside, in one group first, with both bots present","Agreed period answering the same questions from both, with disagreements logged","Announce the switch before removing their bot, not after","Remove the AnyGen bot from each group in the agreed order","Keep their bot reachable read-only for an agreed window in case something was missed"]},
   {n:"Decommission a SaaS, which is not the same as a server",k:["Revoke every OAuth grant AnyGen holds - Gmail, Drive, Calendar, Sheets, Docs and the rest - because cancelling a subscription does not revoke them","Remove the AnyGen app from the Lark tenant, not just from the groups","Export anything with retention value before the account closes, since access ends with billing","Cancel the subscription only after the export is verified restorable","Record the date access actually ended, which is a different date from the last invoice"]}
 ]},
 {n:"Pilot and rollout",s:[
   {n:"Pilot",k:["One department, agreed scope, named champion","Twenty real questions as the acceptance set","Daily review of every answer for the first week","Explicit go or no-go decision with written criteria"]},
   {n:"Departmental rollout",k:["Rollout order agreed with the client","Per-department onboarding session","Per-department starter questions and knowledge seeding","Adoption measured as real questions from real people, machine traffic excluded"]},
   {n:"Training",k:["Staff session: what it can see, how to ask, how to correct it","Admin session: console walkthrough, grants, leashes, review queues","Recorded and left with the client"]}
 ]},
 {n:"Acceptance and commercial close",s:[
   {n:"Acceptance",k:["Acceptance criteria signed before build, not after","Invariant suite green, evidenced","Restore drill executed in front of the client","Security review completed if the client requires one","Penetration test scheduled where contractually required"]},
   {n:"Contract artifacts",k:["Data processing agreement","Service level statement with the RPO and RTO from the chosen profile","Support and escalation procedure","Subprocessor list covering every model provider used"]},
   {n:"Handover",k:["Runbook per console screen","Incident playbook per failure mode","Named client owner for knowledge, grants and connectors","Scheduled job registry handed over with what each one guards","Thirty-day post-launch review booked at handover"]}
 ]},
 {n:"Operational readiness",s:[
   {n:"Scheduled job registry",k:["Every scheduled control listed with what it guards","CI assertion that each control appears in the scheduler","Alert when a scheduled job has not run within its window","Explicit test that no safety mechanism lacks a caller"]},
   {n:"On-call",k:["Alert routing and severity definitions","Runbook per alert","Escalation path to us where the client cannot resolve it"]},
   {n:"Cost validation",k:["Measured spend against the projection after thirty days","Estimator correction from actuals","Budget thresholds retuned with real distribution rather than assumptions"]}
 ]}
]}
];
