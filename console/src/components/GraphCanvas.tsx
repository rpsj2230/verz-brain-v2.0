/**
 * The only place this console mounts React Flow. Read-only by construction.
 *
 * **One mount, for the same reason there is one `fetch`.** A trace graph over a completed run
 * and a canvas somebody is drawing a procedure on are different screens with different
 * grammars, and they are the same drawing surface. Two mounts would be two sets of
 * interaction flags, and the second one is where somebody leaves `nodesConnectable` on
 * because it was the default.
 *
 * **Read-only is a shape here, not a setting.** The nodes and the edges are passed as props
 * with no `onNodesChange` and no `onEdgesChange`, which is React Flow's controlled mode: there
 * is nowhere for a change to go. The interaction flags below are belt and braces on top of
 * that, and they are what stops a node carrying a `tabindex`, a drag handler and a
 * `selectable` class that says it can be acted on when it cannot. The distinction matters
 * because a control that looks live and does nothing is read by the person using it as a
 * permission problem.
 *
 * **A node is drawn from `graph.ts` and never from a measurement.** Positions come from
 * `layout`, which packs each row of the graph it was given, so a step this caller may not see
 * leaves no gap. The argument for that, and for dropping an edge whose other end did not
 * arrive, is in `graph.ts`; it is the whole reason a graph needs its own reader.
 *
 * **The stylesheet is `base.css`, not `style.css`.** The first is the mechanics: transforms,
 * stacking, the pane and the viewport. The second adds a visual theme with its own colours,
 * which would be a second palette in a project whose entire theme is one file of tokens. The
 * node's appearance is `styles/app.css` and the two library variables it needs, the edge
 * stroke and the handle, are pointed at tokens there.
 *
 * **Nothing here fetches and nothing here decides.** A graph arrives as a value; see
 * `graph.ts` for the shape and for the fact that no route sends one yet.
 *
 * Task ids: M32.5.2.3
 */

import {
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { Chip } from "../ui/Chip";
import { layout, type Graph } from "./graph";

/** What one node carries into the drawing surface. The same three names `graph.ts` reads. */
type StepData = { readonly label: string; readonly kind: string };
type StepNodeShape = Node<StepData, "step">;

/**
 * One step on the canvas.
 *
 * **Its class list is a constant.** There is no `graph-node--denied`, no
 * `graph-node--restricted` and no modifier of any kind, for the reason `.lock` has none and
 * `.badge` has exactly four tones named after loudness: a node that looked different when the
 * reason was one thing rather than another would let two people comparing screens read the
 * difference off. The kind is shown as text in a chip, which has one appearance, so a canvas
 * cannot decide that some of its steps are alarming.
 *
 * The handles exist because an edge needs somewhere to attach and for no other reason. They
 * are not connectable: a drawing surface that could be wired up by dragging is an editor, and
 * a trace of something that already happened is not editable.
 */
function StepNode({ data }: NodeProps<StepNodeShape>) {
  return (
    <div className="graph-node">
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <span className="graph-node__label">{data.label}</span>
      {data.kind === "" ? null : <Chip label={data.kind} />}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

/**
 * The node types this console has: one.
 *
 * Declared at module level because React Flow warns when it is handed a new object on every
 * render, and because a registry built inside a component is a registry a component can
 * decide the contents of.
 */
const NODE_TYPES: NodeTypes = { step: StepNode };

interface GraphCanvasProps {
  /** What this canvas shows, for a screen reader. */
  readonly label: string;
  /** The graph, already read and pruned by `readGraph`. Never a raw payload. */
  readonly graph: Graph;
}

export function GraphCanvas({ label, graph }: GraphCanvasProps) {
  const placement = layout(graph);
  const nodes: StepNodeShape[] = graph.nodes.map((node) => ({
    id: node.id,
    type: "step",
    position: placement.get(node.id) ?? { x: 0, y: 0 },
    data: { label: node.label, kind: node.kind },
  }));
  const edges: Edge[] = graph.edges.map((edge) => ({
    id: `${edge.from} ${edge.to}`,
    source: edge.from,
    target: edge.to,
  }));

  return (
    <div className="graph__canvas" role="group" aria-label={label}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        // Controlled with no change handlers, so nothing that happens on the surface can
        // reach the graph. Each flag below removes a way the surface would otherwise say it
        // could be acted on.
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        edgesReconnectable={false}
        elementsSelectable={false}
        fitView
      />
    </div>
  );
}
