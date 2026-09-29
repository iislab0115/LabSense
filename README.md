# LabSense — data collection software

Software used to collect and prepare **LabSense**, a multimodal ambient sensor dataset for activity,
occupancy and energy analysis in an office workspace, described in the accompanying Data Descriptor.

This repository consolidates the three tools that were previously maintained separately. Each tool
keeps its own README, requirements and history; this page explains how they fit together.

| Directory | What it does | Produces |
|---|---|---|
| [`data-collection/`](data-collection) | Polls Shelly smart plugs over the local network at 1 s and SmartThings devices (plugs, motion sensors, door-contact sensors, the stick vacuum, camera event channels) through the SmartThings API at 60 s. Includes a live dashboard and a supervisor that restarts a stalled collector. | `SMP*`, `MS*`, `DS*`, `Camera_*`, `Stick vacuum_*` daily CSV files |
| [`har-datalogger/`](har-datalogger) | Application the three primary participants used to record activity transitions in real time, with automated refinement of the raw self-report log. | the `START`/`STOP` candidate intervals behind `activity_*.csv` |
| [`homecam-media-converter/`](homecam-media-converter) | Converts CN Core HomeCam `.media` files to MP4 (H.264 + AAC). | the reference video used for annotation, which is **not** part of the public dataset |

## Relationship to the dataset

The released dataset is deposited separately, because it is 1.96 GB and is versioned and cited on its
own. This repository is the software; the dataset is the data.

- Dataset: [DOI URL] — `TO BE ASSIGNED ON DEPOSIT`
- Data Descriptor: `TO BE ASSIGNED ON PUBLICATION`

Nothing in this repository is required to *use* the dataset. The released CSV files are
self-describing and documented by the data dictionary that ships with them. This code is here so that
the acquisition can be audited and reproduced.

## What is deliberately not here

No credentials, tokens, OAuth files, collected measurements or participant identifiers are committed.
`data-collection/config.json` is a placeholder template; real values belong in `config.local.json`,
which is git-ignored. Raw video and audio are likewise absent — the converter is included, the media
are not.

## Licence

MIT, for all three tools. See [`LICENSE`](LICENSE) and the per-directory licence files where present.
The **dataset** is released separately under CC BY 4.0; the two licences are not interchangeable.

## Upstream history

Each directory was imported with `git subtree`, so its upstream commits are preserved in this
repository's history. The original repositories are:

- `data-collection/` ← <https://github.com/lime9903/data-collection>
- `har-datalogger/` ← <https://github.com/jyoung531/HAR-DataLogger>
- `homecam-media-converter/` ← <https://github.com/haewonlii/homecam-media-converter>

Those repositories are superseded by this one. To pull a later upstream change into a directory:

```bash
git subtree pull --prefix=data-collection https://github.com/lime9903/data-collection.git main --squash
```

## Citation

If you use this software, cite it through [`CITATION.cff`](CITATION.cff), and cite the dataset by its
own DOI.
