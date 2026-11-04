"""Log instrument runs to the DDOM Lab eLabFTW.

Quick start:

```python
from ddomlab_eln import ELN

eln = ELN()                       # reads ELN_API_KEY from the environment
t = eln.get_template(18)
print(list(t.fields))
```
"""
from .client import (
    ELN,
    Attachment,
    ELNError,
    Experiment,
    ExperimentSummary,
    LinkedExperiment,
    Links,
    Resource,
    Template,
    show_experiments,
)

__all__ = [
    "ELN",
    "ELNError",
    "Attachment",
    "Experiment",
    "ExperimentSummary",
    "LinkedExperiment",
    "Links",
    "Resource",
    "Template",
    "show_experiments",
]
