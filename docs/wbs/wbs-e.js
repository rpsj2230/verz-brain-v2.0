module.exports = [
{id:"M38",name:"Continuous delivery and live status",wave:0,tasks:[
 {n:"Pipeline, built in wave 0 so every wave can ship",s:[
   {n:"Laptop to GitHub",k:["Branch per track named for its module, so twelve concurrent tracks do not collide","Commit message convention carrying the task id, for example M3.2.1","Pre-push hook running lint, types and the invariant suite locally","Pull request template listing the task ids the branch closes","CODEOWNERS forcing review on the gate, the redactor and the catalogue projection"]},
   {n:"GitHub to image",k:["Actions workflow: lint, type check, unit tests, invariant suite, schema sweeps","Migration forward and rollback against a production-shaped snapshot","Container image build with the commit SHA as the tag","Image signing","Push to the registry"]},
   {n:"Image to server",k:["Coolify deployment triggered on a tagged release","Migrations applied before the new image takes traffic","Health gate: readiness must pass before the old container stops","Automatic rollback if readiness fails within the window","Deployment record written to the ledger with the SHA and the task ids in that release"]},
   {n:"Environment ladder",k:["Local compose for development","Staging on the same server, separate compose project and database","Production, deployed only from a tagged release that passed staging","Seed data in staging, never production data"]}
 ]},
 {n:"Deploy at the end of every wave",s:[
   {n:"Wave close ritual",k:["Tag a release at the end of each wave","Run the full invariant suite against staging","Deploy to production","Smoke test: one real question answered end to end by a real person","Restore drill from wave three onward","Wave report generated automatically from the closed task ids"]},
   {n:"What is live after each wave",k:["W0: nothing user-facing, but the pipeline itself deploys and is provably working","W1: a person asks in the console and gets a permission-correct answer from seeded data","W2: the same question answered in Lark, against connector cassettes","W3: agents installed from templates, answering with knowledge and memory","W4: automations running, approvals landing in Lark","W5: real connector credentials wired, browser tasks, go-live"]}
 ]},
 {n:"Live progress, generated not typed",s:[
   {n:"Status from git",k:["CI step parsing every commit message for task ids","Status file written to the repository on each merge to main","Task marked done only when its branch merges and CI is green, never by hand","Percentage, per-wave and per-module progress computed from that file"]},
   {n:"Status endpoint on the server",k:["Status page served by the application itself at a fixed path","Reads the status file baked into the image at build time","Shows overall percentage, current wave, tasks closed today, and what is next","Shows the deployed commit SHA and when it shipped","Visible to the client so progress needs no meeting"]},
   {n:"Daily digest",k:["Automated message to a Lark channel each evening","Tasks closed, tasks opened, anything overdue","Wave burn-down against the target date","Sent by the system itself once W2 has shipped the Lark channel"]}
 ]},
 {n:"Connector wiring at go-live",s:[
   {n:"Coded in wave two, wired in wave five",k:["Every connector built and tested against recorded cassettes, so credentials never block development","Contract tests proving the adapter matches the recorded shape","Credential slots defined in OpenBao with the scopes each connector needs, empty until go-live"]},
   {n:"Go-live wiring",k:["Obtain and store real credentials per connector","Replay the contract tests against the live API and diff against the cassette","Rate-limit ceilings confirmed against the live account tier, not the documentation","First sync run with the projection populated and verified","Cassettes refreshed from live so future development stays faithful"]}
 ]}
]}
];
