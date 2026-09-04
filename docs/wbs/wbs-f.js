module.exports = [
{id:"M39",name:"Agent workspace, the inside-an-agent surface",wave:3,tasks:[
 {n:"The agent as a container, not a config row",s:[
   {n:"Composition model",k:["An agent record composes persona, knowledge scope, connectors, skills, channels, availability, leash, model policy, memory and automations","Every attachment stored as a reference with a pinned version, never an inline copy","Attachment add and remove written to the ledger with who, when and why","Divergence flag when an instance edits something its template supplies","Composition diff between an instance and its template, rendered side by side"]},
   {n:"Workspace shell",k:["Agent header: avatar, name, role line, owner, template lineage and version","Tab strip: Conversations, Automations, People, Knowledge, Memory, Artifacts, Settings","Right pane switches between Dashboard and Profile without losing tab state","Deep link to any tab so a console alert can point at the exact place","Keyboard navigation across tabs and back to the roster"]},
   {n:"Cost on the front page",k:["Spend, runs and messages as the agent's headline figures, not a buried report","Range selector across seven, thirty and ninety days and month to date","Cost attributed per caller so a single heavy user is visible","Projection to month end against the agent's own budget"]}
 ]},
 {n:"Capabilities tab, what the agent is assembled from",s:[
   {n:"Connectors on the agent",k:["Connector row with per-source icons and an overflow count","Attach and detach with the capability check performed at attach time","Each connector shows which fields it projects for this agent","A connector the agent asks for but the department has not granted shown as requested, not attached","Health inherited from the connector page so a degraded source is visible here"]},
   {n:"Skills on the agent",k:["Skill chips with version, source and review state","Attach only from the approved library, never directly from a repository","Unapproved skill offered with a route to the review queue rather than an attach button","Per-skill invocation count so unused attachments can be pruned","Skill description convention enforced: the description is the router prompt"]},
   {n:"Knowledge on the agent",k:["Knowledge scope expressed as a predicate, not a document list","Add by upload, by link extraction, or by widening the predicate","Preview of how many items the predicate currently matches","Coverage and staleness for exactly this agent's slice","Items the agent retrieved most and items it never touched"]},
   {n:"Channels on the agent",k:["Channel row with per-channel enable","Channel capability intersected with the agent ceiling before it is offered","Per-channel rendering profile chosen automatically","Group installation where the channel supports it"]}
 ]},
 {n:"Availability against ceiling, kept visually distinct",s:[
   {n:"Two different questions, two different blocks",k:["Availability block: which users and departments can find and invoke this agent","Ceiling block: the widest entitlement any run of this agent may reach","Explicit copy stating that availability is discovery and never authority","Worked preview: pick a person, see exactly what their run of this agent would return","Preview computed by the real gate, never by a separate estimation path"]},
   {n:"Leash on the agent",k:["Leash matrix per target and scope, read, draft and write on separate rungs","Promotion requires evidence: clean runs, agreement rate and a named approver","Demotion by circuit breaker shown with the metric that tripped it","Money and irreversibility boundaries pinned so a rung cannot rise without a second approver","History of every promotion and demotion with the evidence attached"]}
 ]},
 {n:"Memory made visible",s:[
   {n:"Memory viewer",k:["Memory rendered as readable text, never an opaque store","Split between curated memory and memory extracted from conversations","Change history with a diff per revision and the trigger that caused it","Direct edit and direct delete by the owner","Per-agent and per-person memory shown separately so neither hides the other"]},
   {n:"Learning on the agent",k:["Which tiers are active for this agent","Tier one changes listed with one-click undo, never an approval queue","Tier two rules shown with their shadow evidence and a promote control","Tier three items routed to the department queue with a link back here","A single control to freeze all learning for this agent during an incident"]}
 ]},
 {n:"Artifacts, what the agent produced",s:[
   {n:"Artifact store",k:["Every produced document, deck, report, export and image recorded as an artifact","Artifact carries producing run, agent version, caller, entitlement hash and timestamp","Stored in the object store with the retention class of its most sensitive input","Field-level redaction applied at production time, so an artifact never exceeds its caller","Re-download re-checks the requester entitlement rather than trusting the original link"]},
   {n:"Artifact surface",k:["Artifacts tab listing what this agent produced, newest first","Filter by type, by caller and by date","Provenance panel: which sources and which knowledge items fed it","Supersede and archive rather than delete","Artifact count and storage shown against the retention policy"]}
 ]},
 {n:"Automations owned by the agent",s:[
   {n:"Automation surface",k:["Automations listed under the agent that owns them, never in a global pile","Named as outcomes in the first person, not as trigger and action mechanics","Template gallery for common automations with a one-step install","Per-automation run history with the last result and the next scheduled time","Pause, resume and delete with the scheduler registry updated in the same transaction"]},
   {n:"Automation safety",k:["An automation runs on a named principal, never on the agent itself","Schedule changes are a tier three learning event and need a human","Failure after a threshold pauses the automation and notifies the owner","Every automation appears in the scheduled job registry with what it guards"]}
 ]}
]},
{id:"M40",name:"Member self-service application",wave:4,tasks:[
 {n:"Sign-in and the member shell",s:[
   {n:"Authentication",k:["Single sign-on through Keycloak with the same identity the channels use","Channel identity and web identity resolved to one principal, never two","Session policy, idle timeout and device list","Password reset and recovery handled by the identity provider, not by us","Explicit test that a channel-only user can sign in and sees the same entitlement"]},
   {n:"Shell",k:["Member navigation distinct from the admin console and much shorter","No administrative surface reachable by URL guessing, enforced server side","Responsive down to a phone since approvals arrive on phones","Entitlement disclosure line rendered from the real entitlement set, never hand-written"]}
 ]},
 {n:"My workspace",s:[
   {n:"Landing",k:["Questions asked this month with corrections separated out","Agents this person can call, split between department-provided and self-built","Personal budget consumed against the personal cap","Count of things learned about this person, each reversible","Recent threads continued from any channel, so web and Lark are one history"]},
   {n:"My agents",k:["List with source, where each one runs and how often this person used it","Open an agent into the same workspace surface, bounded to what this person may see","Build a personal agent from a template within the person's own ceiling","Personal agent visible to nobody else unless explicitly shared","Request an agent the person cannot yet call, routed to the department admin"]}
 ]},
 {n:"My knowledge, skills and artifacts",s:[
   {n:"Knowledge I own",k:["Items this person uploaded with visibility scope, verification date and review due","Upload, replace and retire with the review cycle set at upload time","Usage count so an author can see whether anything reads what they wrote","Request promotion to company-wide, routed as a tier three decision","Never shows an item outside this person's entitlement, including their own department's restricted rows"]},
   {n:"Skills I wrote",k:["Personal skills with source, version and review state","Author in the studio, import from a repository or paste a URL","Submit to the department library for review","Personal skills usable only by this person's own agents until approved"]},
   {n:"My artifacts",k:["Everything produced for this person by any agent","Re-download re-checked against current entitlement","Share with a colleague only where that colleague's entitlement already covers the contents"]}
 ]},
 {n:"What the system knows about me",s:[
   {n:"Learning about me",k:["Plain-language list of every preference learned, with the evidence that produced it","One-click undo per item and an undo-all control","Weekly digest delivered in the person's own channel with the same controls","Opt out of personal learning entirely, with the cost stated honestly"]},
   {n:"My data",k:["What is stored about this person and where, in plain language","My conversation history with export","Deletion request routed to the retention policy rather than performed silently","Which agents have read my HR record, from the audit ledger"]}
 ]},
 {n:"Connections and channels",s:[
   {n:"Connected accounts",k:["Personal account connections with per-source consent","Explicit copy stating that connecting an account never widens what the person may see","Disconnect with immediate token revocation","Department-provided connections shown as inherited and not disconnectable here"]},
   {n:"Channels",k:["Which channels this person is reachable on and which is primary","Per-channel notification preferences","Approval routing preference for envelope decisions","Quiet hours honoured by automations and digests"]}
 ]},
 {n:"Approvals in the member surface",s:[
   {n:"Approval inbox",k:["Envelope approvals awaiting this person, with what will happen stated before the fact","Approve, reject and amend, each written to the ledger","Expiry on an unanswered approval with a stated default of doing nothing","Same approval reachable from Lark and from the web without double action","Mobile layout treated as the primary case for approvals"]}
 ]}
]}
];
