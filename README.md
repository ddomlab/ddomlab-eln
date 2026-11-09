# ddomlab-eln

Log instrument runs to the DDOM Lab eLabFTW from your own Python scripts: create an experiment from a template, fill in its fields, add notes and files, and link it to samples, chemicals and earlier runs.

```python
from ddomlab_eln import ELN

eln = ELN()
exp_id = eln.create_experiment(18, "XRD scan S-12", {"Sample ID": "S-12", "Scan Rate": 2})
eln.upload_file(exp_id, "run43.xy", comment="raw scan")
```

## Install

You need Python 3.11 or newer. Check with `python --version`.

```bash
pip install "git+https://github.com/ddomlab/ddomlab-eln@v0.1.0"
```

## Set up your API key

The package acts as you, using your personal eLabFTW API key.

1. In the ELN, open the user menu at the top right, then **Settings**, then the **API keys** tab.
2. Enter a name (for example, the instrument's name), change **Permissions** from Read Only to **Read/Write**, and create the key.
3. Copy the key straight away.
4. Save it as an environment variable called `ELN_API_KEY`, then open a new terminal.

On Windows:

```powershell
setx ELN_API_KEY "paste-your-key-here"
```

On Mac:

```bash
echo 'export ELN_API_KEY="paste-your-key-here"' >> ~/.zshrc
```

**Keep the key secret.** Anyone who has it can act as you in the ELN. Never write it in a script, and never share or commit it. Don't store your personal key on a shared instrument computer until the lab has agreed how those should be set up.

To check that it works:

```bash
python -c "from ddomlab_eln import ELN; print(ELN())"
```

You should see `<ELN https://eln.ddomlab.org as Your Name>`.

## Log your first run

**1. Make a template by hand in the ELN.** Every experiment created by this package starts from a template. Add a field for each thing you want to record, mark the ones that must be filled in as required, and drag the fields into the order you want.

**2. Find the template's ID.**

```python
from ddomlab_eln import ELN

eln = ELN()
for template in eln.list_templates():
    print(template.id, template.title, list(template.fields))
```

**3. Write your script.** Use your own template's ID and field names in place of the 18 and the fields below. Field names must match the template exactly.

```python
exp_id = eln.create_experiment(18, "XRD scan S-12", {
    "Sample ID": "S-12",
    "Scan Rate": 2,
    "Mode": "slow",
})
eln.add_note(exp_id, "Run finished at 14:02. Peak at 28.4 degrees.")
eln.upload_file(exp_id, "run43.xy", comment="raw scan")
eln.add_tags(exp_id, ["XRD", "package-test"])
print(f"Logged as experiment #{exp_id}")
```

## What you can do

| To do this | Use |
|---|---|
| **Templates** | |
| List the templates you can use | `eln.list_templates()` |
| Read one template and its fields | `eln.get_template(template_id)` |
| **Experiments** | |
| Create an experiment from a template | `eln.create_experiment(template_id, title, fields)` |
| Read a whole experiment | `eln.get_experiment(exp_id)` |
| Search experiments | `eln.find_experiments(...)` |
| Print search results as a table | `show_experiments(results)` |
| **Fields** | |
| Change field values | `eln.update_fields(exp_id, {"Mode": "fast"})` |
| Add a new field to one experiment | `eln.add_field(exp_id, name, value, field_type="number", unit="C")` |
| **Text, files and tags** | |
| Add to the end of the main text | `eln.add_note(exp_id, text)` |
| Replace the whole main text | `eln.set_text(exp_id, text, overwrite=True)` |
| Attach a file | `eln.upload_file(exp_id, path, comment="...")` |
| Add tags | `eln.add_tags(exp_id, ["XRD", "P3HT"])` |
| **Links** | |
| Find a resource's ID | `eln.find_resources("acetone")` |
| Link a resource | `eln.link_resource(exp_id, resource_id)` |
| Link another experiment | `eln.link_experiment(exp_id, other_id)` |
| See what's linked | `eln.get_links(exp_id)` |

For the details of any function, use `help()` in Python, for example `help(ELN.find_experiments)`.

## Find experiments and link them

With no options, `find_experiments` returns your own 20 newest experiments. Combine any of these options:

| Option | Example | Finds |
|---|---|---|
| `text` | `"P3HT resistance"` | All these words in the title or main text |
| `fields` | `{"Sample ID": "S-12"}` | That exact value; `%` means "anything", as in `"S-%"` |
| `since`, `until` | `since="2026-10-01"` | Experiments in a date range |
| `template_id` | `18` | Runs created from that template; most older experiments don't record one |
| `tags` | `["XRD", "P3HT"]` | Experiments with all these tags |
| `status` | `"Success"` | Experiments with exactly this status |
| `owner` | `9` or `"all"` | A colleague's experiments by user ID, or everyone's |
| `limit` | `50` | The most results to return |

To choose by eye:

```python
from ddomlab_eln import show_experiments

show_experiments(eln.find_experiments("XRD", since="2026-10-01"))
```

To link the previous run on the same sample automatically:

```python
earlier = eln.find_experiments(fields={"Sample ID": "S-12"}, limit=2)
for run in earlier:
    if run.id != exp_id:
        eln.link_experiment(exp_id, run.id)
```

To link a chemical or sample, look up its ID once and use that ID in your script. Several resources can share a name, so check the title and category:

```python
for resource in eln.find_resources("acetone"):
    print(resource.id, resource.title, resource.category)

eln.link_resource(exp_id, 562)
```

## When something goes wrong

Every problem raises an `ELNError` with a sentence saying what happened. If logging fails, your instrument run shouldn't be lost with it, so catch the error and keep your data:

```python
from ddomlab_eln import ELNError

try:
    eln.update_fields(exp_id, {"Mode": "fast"})
except ELNError as error:
    print(f"Couldn't log this to the ELN: {error}")
```

Good to know:

- **Field checks.** Misspelled field names and empty required fields are refused before anything is saved. If a value is refused, none of the values in that call are saved.
- **Main text.** `add_note` never removes anything. `set_text` replaces everything, and the old text may not be recoverable, so it refuses to overwrite existing text unless you pass `overwrite=True`.
- **Re-running a script.** Uploading an identical file again is skipped, and linking twice is harmless. Calling `create_experiment` again creates another experiment.
- **File size.** The ELN accepts files up to 100 MB.
- **Anything not covered here.** `eln.client` is the official `elabapi-python` connection, already logged in.

## For developers

```bash
uv sync
uv run pytest
```

Without further setup, `pytest` runs the tests that need no server. To run all of them, start a local copy of the ELN and point the tests at it. They refuse any address that isn't on your own computer.

```bash
ELN_TEST_URL=http://localhost:8088 ELN_API_KEY=key-for-the-local-copy uv run pytest
```

To preview the reference documentation, generated from the docstrings:

```bash
uv run pdoc ddomlab_eln -d google --no-show-source
```
