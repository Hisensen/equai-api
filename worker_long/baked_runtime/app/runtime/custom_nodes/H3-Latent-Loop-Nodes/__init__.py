from .auto_nodes import H3AudioWindowCrop, H3ContinuationPassCount, H3ContinuationWindowPlan
from .loop_nodes import H3ContinuationLoopEnd, H3ContinuationLoopStart

NODE_CLASS_MAPPINGS = {
    "H3ContinuationPassCount": H3ContinuationPassCount,
    "H3ContinuationWindowPlan": H3ContinuationWindowPlan,
    "H3AudioWindowCrop": H3AudioWindowCrop,
    "H3ContinuationLoopStart": H3ContinuationLoopStart,
    "H3ContinuationLoopEnd": H3ContinuationLoopEnd,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "H3ContinuationPassCount": "H3 自动计算续接轮数",
    "H3ContinuationWindowPlan": "H3 latent 窗口规划",
    "H3AudioWindowCrop": "H3 自动音频窗口（补齐静音）",
    "H3ContinuationLoopStart": "H3 连续 latent 循环开始",
    "H3ContinuationLoopEnd": "H3 连续 latent 循环结束",
}
