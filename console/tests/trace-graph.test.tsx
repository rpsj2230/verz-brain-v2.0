/**
 * The trace graph: what reaches the canvas, and the two shapes an absent step must not leave.
 *
 * A step in a run is a record, so the record-level rule applies to it and not the field-level
 * one. `brain.core.redaction` argues the two separately: a field inside a record whose
 * existence was already disclosed renders a lock, and a whole record the caller may not see is
 * dropped rather than emptied, because returning the husk announces that it exists. So there
 * is no locked node, no dimmed node and no "restricted step" on this canvas.
 *
 * Two shapes are how an absent step would announce itself anyway, and neither is anything
 * somebody would write deliberately.
 *
 * **An edge whose other end did not arrive is an arrow that ends nowhere.** A payload filtered
 * for a caller can carry one easily, because the edge is a fact about the step they hold.
 * Drawn, it says a step is there.
 *
 * **A layout that reserved a place for every node an edge mentioned would leave a hole.** The
 * hole is where the withheld step was and it is readable. So the placement is asserted to be a
 * function of what arrived and of nothing else.
 *
 * The canvas is exercised under jsdom, which has no layout: a node's position, its size and
 * whether an edge is drawn between two of them cannot be observed here. What can be observed
 * is which nodes and which text reach the DOM, and that is what these tests read. The
 * placement rules are checked on `layout` directly, where they are arithmetic rather than
 * pixels. See `console/README.md` for what that leaves unverified.
 *
 * Task ids: M32.5.2.3
 */

import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { DataTable, type GridColumn } from "../src/components/DataTable";
import {
  COLUMN_GAP,
  EMPTY_GRAPH,
  NODE_WIDTH,
  ROW_GAP,
  UnreadableGraph,
  layout,
  readGraph,
  type Graph,
} from "../src/components/graph";
import { TraceGraph } from "../src/components/TraceGraph";
import { customProperties, parseCss, type CssRule } from "./support/css";
import { backendPublicMessages, backendSpanFields } from "./support/python";
import { readConsoleFile } from "./support/repo";
import { jsxAttributeUses, parseConsoleSource } from "./support/typescript";

const CANVAS_MODULE = "src/components/GraphCanvas.tsx";

/**
 * A run of five steps as the API computed it, and the three of them one caller may see.
 *
 * `identify` and `entitle` are absent from `nodes` because this caller may not see them, and
 * the edges still mention them, which is the shape a serialiser copying edges off the visible
 * steps produces. No label here carries a digit.
 */
const PARTIAL_RUN = {
  nodes: [
    { id: "ingress", label: "Ingress", kind: "gate" },
    { id: "route", label: "Route", kind: "gate" },
    { id: "invoke", label: "Invoke", kind: "tool" },
  ],
  edges: [
    { from: "ingress", to: "identify" },
    { from: "identify", to: "entitle" },
    { from: "entitle", to: "route" },
    { from: "ingress", to: "route" },
    { from: "route", to: "invoke" },
  ],
};

/** The same three steps with nothing dangling, as a caller who may see everything would get. */
const WHOLE_RUN = {
  nodes: PARTIAL_RUN.nodes,
  edges: [
    { from: "ingress", to: "route" },
    { from: "route", to: "invoke" },
  ],
};

function nodeIdsOn(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".react-flow__node")].map(
    (node) => node.getAttribute("data-id") ?? "",
  );
}

function tokenRules(): CssRule[] {
  return parseCss(readConsoleFile("src/theme/tokens.css"));
}

describe("reading a graph", () => {
  test("an edge to a step that did not arrive is dropped with the step", () => {
    // What breaks if this is deleted: the disclosure this module exists for. An edge from a
    // step this caller holds to one they do not is a fact about the step they hold, so a
    // serialiser emits it without anybody deciding to. Drawn, it is an arrow leaving a node
    // and ending nowhere, which says a step is there and that they may not see it. No drawing
    // library will drop it for you: they either draw it to a stub or fail.
    const graph = readGraph(PARTIAL_RUN);

    expect(graph.nodes.map((node) => node.id)).toEqual(["ingress", "route", "invoke"]);
    expect(graph.edges).toEqual([
      { from: "ingress", to: "route" },
      { from: "route", to: "invoke" },
    ]);
    expect(JSON.stringify(graph)).not.toContain("identify");
    expect(JSON.stringify(graph)).not.toContain("entitle");
  });

  test("a graph carries a step's name and kind and nothing a span records", () => {
    // What breaks if this is deleted: the question and the answer arrive on a canvas.
    // `brain.ops.tracing.Span` keeps `payload_in` and `payload_out` as separate fields
    // precisely because they are the two that can never be allowlisted, and `mask` masks them
    // before a span leaves the process. A reader that copied a payload field would draw the
    // masked question on a screen, and a reader written against an older shape would draw the
    // unmasked one. The field names are read out of the Python source, so this fails if the
    // span ever grows another field of that kind.
    const declared = backendSpanFields();
    expect(declared).toContain("payload_in");
    expect(declared).toContain("payload_out");

    const graph = readGraph({
      nodes: [
        {
          id: "invoke",
          label: "Invoke",
          kind: "tool",
          payload_in: "what was asked",
          payload_out: "what was answered",
          attributes: { "gate.decision": "allowed" },
        },
      ],
    });

    expect(graph.nodes).toEqual([{ id: "invoke", label: "Invoke", kind: "tool" }]);
    const drawn = JSON.stringify(graph);
    for (const field of declared.filter((name) => name.startsWith("payload"))) {
      expect(drawn).not.toContain(field);
    }
    expect(drawn).not.toContain("what was asked");
    expect(drawn).not.toContain("allowed");
  });

  test("a count the API sends reaches nothing", () => {
    // What breaks if this is deleted: "showing 3 steps of 5". The disclosing number does not
    // have to be a count of hidden things: a total the API was entitled to compute, beside a
    // canvas of the steps this caller may see, gives the difference away by subtraction. The
    // value stops in this reader, so there is nothing to render rather than an agreement not
    // to render it.
    const graph = readGraph({ ...PARTIAL_RUN, total: 4700, hidden: 2 });
    expect(JSON.stringify(graph)).not.toContain("4700");
    expect(JSON.stringify(graph)).not.toContain("hidden");
  });

  test("a body that is not a graph is a failure rather than an empty graph", () => {
    // What breaks if this is deleted: a broken endpoint renders as a canvas with nothing on
    // it, and a person reads that as a run that did nothing. An empty graph is a legitimate
    // answer about somebody's data and a malformed body is a bug, and showing the second as
    // the first is the console saying something about a run that nobody said.
    expect(() => readGraph(null)).toThrow(UnreadableGraph);
    expect(() => readGraph({ edges: [] })).toThrow(UnreadableGraph);
    expect(() => readGraph({ nodes: [], edges: "later" })).toThrow(UnreadableGraph);
    expect(readGraph({ nodes: [] })).toEqual(EMPTY_GRAPH);
  });

  test("a step with no id is not given one", () => {
    // What breaks if this is deleted: an invented id matches an edge the API never meant,
    // which draws a line between two steps that are not connected. A trace read wrongly is
    // worse than a trace not read, because it is read as evidence. A repeated id is dropped
    // for the same reason: two steps sharing one identifier collapse into whichever the
    // renderer saw last.
    const graph = readGraph({
      nodes: [{ label: "Nameless" }, { id: "route", label: "Route" }, { id: "route", label: "Again" }],
      edges: [{ from: "route", to: "route" }],
    });

    expect(graph.nodes).toEqual([{ id: "route", label: "Route", kind: "" }]);
    expect(graph.nodes).toHaveLength(1);
  });
});

describe("where a step goes", () => {
  test("the placement of what arrived does not depend on what did not", () => {
    // What breaks if this is deleted: the sharpest form of a placeholder, which is not an
    // element but a space. A layout that gave every node an edge mentioned a column, and drew
    // only the nodes it had, would leave a gap exactly where the withheld step was, and the
    // gap is readable by anybody who has seen the whole run. Two callers entitled to different
    // subsets must each get a packed graph.
    expect(layout(readGraph(PARTIAL_RUN))).toEqual(layout(readGraph(WHOLE_RUN)));
  });

  test("steps at one depth are packed with no space between them", () => {
    // What breaks if this is deleted: the same hole arrives from the other direction, through
    // a column index taken from the payload's own ordering rather than counted here. Three
    // steps that all follow the first sit at columns nought, one and two, whatever else the
    // payload said about them.
    const graph = readGraph({
      nodes: [
        { id: "root", label: "Root" },
        { id: "a", label: "A" },
        { id: "b", label: "B" },
        { id: "c", label: "C" },
      ],
      edges: [
        { from: "root", to: "a" },
        { from: "root", to: "b" },
        { from: "root", to: "c" },
      ],
    });
    const placed = layout(graph);

    expect(placed.get("a")).toEqual({ x: 0, y: ROW_GAP });
    expect(placed.get("b")).toEqual({ x: NODE_WIDTH + COLUMN_GAP, y: ROW_GAP });
    expect(placed.get("c")).toEqual({ x: 2 * (NODE_WIDTH + COLUMN_GAP), y: ROW_GAP });
  });

  test("a step sits below every step it followed", () => {
    // What breaks if this is deleted: everything else here is satisfied by a layout that puts
    // every node in one place. This is the positive sibling and it is the only thing that
    // makes the drawing a trace rather than a heap: a step is lower than anything that led to
    // it, including along the longest of several paths.
    const placed = layout(readGraph(WHOLE_RUN));
    const ingress = placed.get("ingress");
    const route = placed.get("route");
    const invoke = placed.get("invoke");

    expect(ingress?.y).toBe(0);
    expect(route?.y).toBeGreaterThan(ingress?.y ?? 0);
    expect(invoke?.y).toBeGreaterThan(route?.y ?? 0);
  });

  test("a payload that loops back on itself still settles", () => {
    // What breaks if this is deleted: a canvas that hangs. A completed trace is a tree and
    // should never contain a cycle, and a procedure somebody is halfway through drawing can,
    // so the relaxation is bounded rather than run to a fixed point. A renderer that looped
    // here would look like a browser tab that had crashed.
    const placed = layout(readGraph({
      nodes: [{ id: "a", label: "A" }, { id: "b", label: "B" }],
      edges: [{ from: "a", to: "b" }, { from: "b", to: "a" }],
    }));

    expect([...placed.keys()].sort()).toEqual(["a", "b"]);
  });

  test("the width the layout spaces by is the width the stylesheet draws", () => {
    // What breaks if this is deleted: the two halves of a node's width drift apart. The
    // placement is arithmetic in a coordinate space and the drawing is a stylesheet rule, and
    // a node drawn wider than the columns were spaced overlaps its neighbour. Reading the
    // token out of the stylesheet rather than asserting a number twice is what makes this a
    // check rather than a restatement.
    const root = tokenRules().find((rule) => rule.atRule === "" && rule.selector === ":root");
    const declared = customProperties(root as CssRule)["--canvas-node-width"];
    expect(declared).toBe(`${NODE_WIDTH}px`);
  });
});

describe("what the canvas draws", () => {
  test("a step the caller may not see has no placeholder on the canvas", () => {
    // What breaks if this is deleted: a dimmed node, an outline, a node captioned with the
    // lock, or a node whose label is a raw identifier out of a dropped edge. A step is a
    // record, and a record the caller may not see is dropped rather than emptied, because the
    // husk announces that it exists. This asserts on what reaches the DOM rather than on what
    // is visible, so a placeholder hidden by a stylesheet would still fail.
    const container = render(
      <TraceGraph caption="A run" graph={readGraph(PARTIAL_RUN)} />,
    ).container;

    expect(nodeIdsOn(container)).toEqual(["ingress", "route", "invoke"]);
    expect(container.innerHTML).not.toContain("identify");
    expect(container.innerHTML).not.toContain("entitle");
  });

  test("nothing on the canvas is a count of steps", () => {
    // What breaks if this is deleted: "3 steps", which is the row count of the grid's footer
    // wearing different clothes. The fixture's labels carry no digits, so any digit in the
    // text on the screen came from the canvas rather than from the run. Attributes are not
    // read, because a coordinate space is made of numbers and none of them is about how many
    // steps there were.
    const container = render(
      <TraceGraph caption="A run" graph={readGraph(PARTIAL_RUN)} />,
    ).container;

    expect(container.textContent ?? "").not.toMatch(/\d/);
  });

  test("a step's appearance does not depend on what kind of step it is", () => {
    // What breaks if this is deleted: `.lock--out-of-scope` drawn on a canvas. A node that
    // looked different for one kind than another is a colour chosen from a value, outside the
    // one module allowed to choose one, and a canvas is where a designer reaches for that
    // first. The kind is shown as text in a chip, which has one appearance, so the class list
    // has to be a constant.
    const classesFor = (kind: string): (string | null)[] => {
      const graph: Graph = { nodes: [{ id: "one", label: "One", kind }], edges: [] };
      const container = render(<TraceGraph caption="A run" graph={graph} />).container;
      return [...container.querySelectorAll(".graph-node")].map((node) =>
        node.getAttribute("class"),
      );
    };

    expect(classesFor("gate")).toEqual(["graph-node"]);
    expect(classesFor("tool")).toEqual(["graph-node"]);
    expect(classesFor("")).toEqual(["graph-node"]);
  });

  test("the canvas cannot be dragged, connected, selected or reached by tab", () => {
    // What breaks if this is deleted: a trace of something that already happened becomes an
    // editor. The mechanism is that the nodes and edges are props with no change handlers, so
    // a change has nowhere to go; the flags are what stop the surface from saying it can be
    // acted on when it cannot, and a control that looks live and does nothing is read as a
    // permission problem. The source is parsed rather than searched, because this file writes
    // out in prose the very flags it sets.
    const source = parseConsoleSource(CANVAS_MODULE);
    for (const attribute of [
      "nodesDraggable",
      "nodesConnectable",
      "nodesFocusable",
      "edgesFocusable",
      "edgesReconnectable",
      "elementsSelectable",
    ]) {
      const uses = jsxAttributeUses(source, attribute);
      expect(uses, attribute).toHaveLength(1);
      expect(uses[0]?.text, attribute).toBe("{false}");
    }
    // And there is nowhere for a change to be sent even if one were made.
    expect(jsxAttributeUses(source, "onNodesChange")).toEqual([]);
    expect(jsxAttributeUses(source, "onEdgesChange")).toEqual([]);

    const container = render(
      <TraceGraph caption="A run" graph={readGraph(WHOLE_RUN)} />,
    ).container;
    const node = container.querySelector(".react-flow__node") as HTMLElement;
    expect(node.getAttribute("class")).not.toContain("draggable");
    expect(node.getAttribute("class")).not.toContain("selectable");
    expect(node.hasAttribute("tabindex")).toBe(false);
  });

  test("every step that arrived is drawn, with the words the API used", () => {
    // What breaks if this is deleted: every test above is satisfied by a canvas that draws
    // nothing at all. This is the positive sibling, and it also holds the rule that the API's
    // own words reach the screen unchanged: prettifying a step name here would mean a
    // screenshot and a support conversation quote a word the API never used.
    const container = render(
      <TraceGraph caption="A run" graph={readGraph(WHOLE_RUN)} />,
    ).container;

    expect(nodeIdsOn(container)).toEqual(["ingress", "route", "invoke"]);
    const labels = [...container.querySelectorAll(".graph-node__label")].map(
      (label) => label.textContent,
    );
    expect(labels).toEqual(["Ingress", "Route", "Invoke"]);
  });

  test("an empty run says exactly what an empty list of records says", () => {
    // What breaks if this is deleted: the canvas explains itself. Empty because the run had no
    // steps, and empty because nothing this caller holds reaches any of them, are the same
    // event, and `brain.app.handle_brain_error` gives them the same status and the same body
    // for that reason. The two sentences are compared as rendered output rather than as a
    // shared constant, so this fails if either screen starts saying something of its own.
    const columns: GridColumn<{ id: string }>[] = [{ id: "id", accessorKey: "id", header: "Id" }];
    const grid = render(
      <DataTable caption="Records" columns={columns} rows={[]} rowId={(row) => row.id} />,
    ).container;
    const canvas = render(<TraceGraph caption="A run" graph={EMPTY_GRAPH} />).container;

    const sentence = grid.querySelector(".grid__empty")?.textContent;
    expect(sentence).toBeTruthy();
    expect(canvas.querySelector(".graph__empty")?.textContent).toBe(sentence);
    // And an empty canvas is not mounted at all: an expanse of blank drawing surface is
    // indistinguishable from one that has not finished loading.
    expect(canvas.querySelector(".react-flow")).toBeNull();
  });

  test("a failure is shown in the API's own words with nothing added", () => {
    // What breaks if this is deleted: the canvas starts speaking for the API about a 404,
    // which is the outcome DENIED and ABSENT share. The sentence is read out of the Python
    // source, so this is not the console's copy of the message compared with itself, and the
    // assertion is that exactly that sentence and nothing else reaches the screen.
    const sentence = backendPublicMessages()["DENIED"];
    expect(sentence).toBeTruthy();

    const container = render(
      <TraceGraph
        caption="A run"
        graph={EMPTY_GRAPH}
        failure={{ status: 404, message: sentence as string, traceId: "trace-abc", outcome: "" }}
      />,
    ).container;

    expect(container.querySelector(".notice__body")?.textContent).toBe(sentence);
    expect(container.querySelector(".graph__empty")).toBeNull();
  });

  test("a run still loading is not reported as a run with no steps", () => {
    // What breaks if this is deleted: every canvas flashes the empty sentence before its first
    // answer arrives. That is a statement about somebody's run made before anybody asked, and
    // on a slow connection it is the sentence they remember.
    const container = render(
      <TraceGraph caption="A run" graph={EMPTY_GRAPH} busy />,
    ).container;

    expect(container.querySelector(".graph__empty")).toBeNull();
    expect(container.querySelector(".graph__busy")).not.toBeNull();
  });
});
