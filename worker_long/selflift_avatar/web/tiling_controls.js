import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const CONTROL_NAMES = new Set(["highres_tiling", "tiling_mode", "tiling_tiles", "tiling_axis"]);

function choices(node) {
    const read = (name, fallback) => node.widgets?.find((w) => w.name === name)?.value ?? fallback;
    if (!read("highres_tiling", false)) return "设置 / Settings: OFF（空间分块关闭）";
    const mode = read("tiling_mode", "auto");
    return `设置 / Settings: ${mode} | axis=${read("tiling_axis", "auto")} | tiles=${mode === "manual" ? read("tiling_tiles", 2) : "auto (1–8)"}`;
}

function actualCount(node) {
    const enabled = node.widgets?.find((w) => w.name === "highres_tiling")?.value ?? false;
    if (!enabled) return "实际分块数量 / Actual tiles: 关闭，不分块 / OFF";
    const count = node.selfliftTilingPlan?.tiles;
    if (!Number.isInteger(count) || count < 1) return "实际分块数量 / Actual tiles: 待估算 / pending (高清准备阶段确定)";
    return `实际分块数量 / Actual tiles: ${count} 块${count === 1 ? "（整图，不拆分 / no split）" : ""}`;
}

function display(node, text, plan) {
    const box = node.selfliftTilingPanel;
    if (!box) return;
    if (text !== undefined) {
        node.selfliftTilingLast = String(text);
        // Explicit null resets a previous run; undefined supports older cached UI text.
        node.selfliftTilingPlan = plan ?? null;
        if (plan === undefined) {
            const match = String(text).match(/Effective \/ 实际:\s*(\d+)/);
            if (match) node.selfliftTilingPlan = { tiles: Number(match[1]) };
        }
    }
    box.value = actualCount(node) + "\n" + choices(node) + "\n" + (node.selfliftTilingLast || "未运行 / Not run. Actual plan appears at high-resolution preparation.\n手动块数仅在 manual 下有效；时间窗口不由这些选项控制。");
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "selflift-Avatar.TilingPlan",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SelfLiftAvatarH3Sampler") return;
        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = created?.apply(this, arguments);
            const box = document.createElement("textarea");
            box.readOnly = true;
            box.setAttribute("aria-label", "selflift-Avatar spatial tiling plan / 空间分块方案");
            box.style.cssText = "width:100%;height:100%;box-sizing:border-box;resize:none;overflow:auto;font:12px/1.45 monospace;color:var(--input-text,#ddd);background:var(--comfy-input-bg,#222);border:1px solid var(--border-color,#555);padding:6px;";
            this.selfliftTilingPanel = box;
            this.addDOMWidget("selflift_tiling_plan", "selflift-tiling-status", box, { serialize: false });
            for (const widget of this.widgets || []) {
                if (!CONTROL_NAMES.has(widget.name)) continue;
                const callback = widget.callback;
                const node = this;
                widget.callback = function () {
                    const value = callback?.apply(this, arguments);
                    display(node, "设置已修改 / Settings changed. Run again to compute a new plan.");
                    return value;
                };
            }
            this.size[0] = Math.max(this.size[0], 370);
            this.size[1] = Math.max(this.size[1], this.computeSize()[1] + 170);
            display(this);
            return result;
        };
        const configured = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = configured?.apply(this, arguments);
            display(this);
            return result;
        };
        const executed = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const result = executed?.apply(this, arguments);
            if (message?.selflift_tiling) display(this, message.selflift_tiling.join("\n"), message.selflift_tiling_plan?.[0]);
            return result;
        };
    },
    setup() {
        api.addEventListener("selflift-avatar-tiling", (event) => {
            const id = String(event.detail?.node_id ?? "");
            // Root-graph events use ordinary IDs. Subgraph UI still receives onExecuted.
            const node = app.graph?.getNodeById(id) ?? (/^\d+$/.test(id) ? app.graph?.getNodeById(Number(id)) : null);
            if (node?.selfliftTilingPanel) display(node, event.detail.text, event.detail.plan);
        });
    },
});
