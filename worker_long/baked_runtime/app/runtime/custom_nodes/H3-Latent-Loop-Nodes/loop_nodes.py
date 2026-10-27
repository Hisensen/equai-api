"""Small fixed-slot latent for-loop nodes using ComfyUI graph expansion."""
from comfy_execution.graph_utils import GraphBuilder, is_link


class H3ContinuationLoopStart:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"count": ("INT", {"default": 1, "min": 1, "max": 64})},
            "optional": {"initial_latent": ("LATENT",)},
            "hidden": {"_current_index": ("INT", {"default": 0}), "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("H3_CONTINUATION_FLOW", "INT", "LATENT")
    RETURN_NAMES = ("flow", "index", "latent")
    FUNCTION = "start"
    CATEGORY = "MiniMax H3/continuation"

    def start(self, count, initial_latent=None, _current_index=0, unique_id=None):
        return ((str(unique_id), int(count)), int(_current_index), initial_latent)


class H3ContinuationLoopEnd:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"flow": ("H3_CONTINUATION_FLOW",)},
            "optional": {"latent": ("LATENT",)},
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "end"
    CATEGORY = "MiniMax H3/continuation"

    def _walk(self, node_id, dynprompt, upstream):
        node_info = dynprompt.get_node(node_id)
        for value in node_info.get("inputs", {}).values():
            if is_link(value):
                parent_id = value[0]
                if parent_id not in upstream:
                    upstream[parent_id] = []
                    self._walk(parent_id, dynprompt, upstream)
                upstream[parent_id].append(node_id)

    def _collect(self, node_id, upstream, contained):
        for child_id in upstream.get(node_id, []):
            if child_id not in contained:
                contained[child_id] = True
                self._collect(child_id, upstream, contained)

    def end(self, flow, latent=None, dynprompt=None, unique_id=None):
        start_id, count = flow
        start_node = dynprompt.get_node(start_id)
        index = int(start_node.get("inputs", {}).get("_current_index", 0))
        next_index = index + 1
        if next_index >= int(count):
            return (latent,)

        upstream = {}
        self._walk(unique_id, dynprompt, upstream)
        contained = {}
        self._collect(start_id, upstream, contained)
        contained[unique_id] = True
        contained[start_id] = True

        graph = GraphBuilder()
        for node_id in contained:
            node_data = dynprompt.get_node(node_id)
            graph.node(node_data["class_type"], "Recurse" if node_id == unique_id else node_id).set_override_display_id(node_id)

        for node_id in contained:
            node_data = dynprompt.get_node(node_id)
            node = graph.lookup_node("Recurse" if node_id == unique_id else node_id)
            for name, value in node_data.get("inputs", {}).items():
                if is_link(value) and value[0] in contained:
                    node.set_input(name, graph.lookup_node(value[0]).out(value[1]))
                else:
                    node.set_input(name, value)

        next_start = graph.lookup_node(start_id)
        next_start.set_input("_current_index", next_index)
        next_start.set_input("initial_latent", latent)
        return {"result": (graph.lookup_node("Recurse").out(0),), "expand": graph.finalize()}
