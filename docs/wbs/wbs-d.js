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
 {n:"Migration from the existing system",s:[
   {n:"Inventory",k:["Catalogue what the current Company Brain holds: knowledge, prompts, tool definitions, conversation history","Decide per item: migrate, rebuild or retire","Map old permissions onto the new capability model, explicitly rather than by assumption"]},
   {n:"Data movement",k:["Knowledge and asset export from the old system","Re-parse and re-embed rather than copying vectors, since embeddings are model-specific","Entity registry seeded from existing client records","Historical conversations archived, not imported, unless there is a stated reason"]},
   {n:"Parallel running",k:["Both systems answering the same questions for an agreed period","Answer comparison log with disagreements surfaced","Cutover criteria agreed in writing before the period starts"]},
   {n:"Decommission",k:["Old system read-only, then archived","Credential revocation on every connector the old system held","Scheduled jobs disabled and verified stopped","Final backup retained per the retention policy"]}
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
