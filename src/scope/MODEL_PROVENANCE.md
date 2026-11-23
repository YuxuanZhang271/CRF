# Gastro28 model provenance

`temporal.py` is adapted from the supplied `endo_agent/temporal_inference.py`
(SHA-256 `01006b31fe3b9e426b04ebb47b88a2271c7238eeb7fa3a134883b6a105b7931d`).
Its model architecture and 512px global/mask token protocol are retained. CRF
shares stage names with its metadata module and permits a standalone dual branch
without loading an otherwise unused primary model in the stage worker.
The neck hook attaches to the initialized predictor's inference model because
the installed Ultralytics version copies the original model during setup.

The paired segmentation and temporal checkpoints came from
`endo_agent/handoff_gastro28_dual_shared_READY/assets/`. `models.json` retains
their original byte sizes and SHA-256 values. The source segmentation path is
`vision_checkpoints/Gastro28/YOLO26/yolo26m-seg/fold0/train/weights/best.pt`;
CRF names this file `models/scope/gastro28_yolo26m_seg.pt`. Both temporal files
retain their original filenames under `models/scope/`. Model files are local,
ignored assets; none is downloaded during a control session.

The supplied handoff package contains no separate license or clinical tip
content. This integration retains the supplied source attribution and does not
introduce a new license for those materials. It does not modify Ultralytics or
the vendored speech SDK.
