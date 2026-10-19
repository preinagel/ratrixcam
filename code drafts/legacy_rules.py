"""Rules specific to the lab's original recording setup, kept apart from the general tools so
they can be removed together once the legacy backlog is done. Nothing in ratrixcam imports
this; videoproc calls into it only where marked."""

TASK_VIEWS = ("lid", "face")
CAGE_VIEWS = ("buddy", "home")
CRF_TASK_MOTION = 25
CRF_CAGE_MOTION = 30


def legacy_crf(view: str) -> tuple[int, bool]:
    """CRF for a clip to be compressed, chosen by the camera-view name: task views and unknown
    views get CRF_TASK_MOTION, cage views CRF_CAGE_MOTION. Prints the choice. Returns
    (crf, whether the view name was unrecognized)."""
    if view in TASK_VIEWS:
        print(f"    task view: minimally lossy compression (CRF {CRF_TASK_MOTION})")
        return CRF_TASK_MOTION, False
    if view in CAGE_VIEWS:
        print(f"    cage view: more lossy compression (CRF {CRF_CAGE_MOTION})")
        return CRF_CAGE_MOTION, False
    print(
        f"    WARNING: unrecognized camera view '{view}' (known: {', '.join(TASK_VIEWS + CAGE_VIEWS)}); "
        f"using minimally lossy compression (CRF {CRF_TASK_MOTION})"
    )
    return CRF_TASK_MOTION, True
