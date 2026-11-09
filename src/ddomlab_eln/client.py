"""The connection to the DDOM Lab eLabFTW."""
import difflib
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import elabapi_python
import urllib3
from elabapi_python.rest import ApiException

DEFAULT_URL = "https://eln.ddomlab.org"
TIMEOUT = (10, 120)  # seconds: to connect, then to wait for each answer
MARKDOWN = 2  # an experiment's content_type: 1 = rich-text editor (HTML), 2 = Markdown
FROM_TEMPLATE = 3  # an experiment's created_from_type: 3 = an experiment template, 1 = another experiment
FIELD_TYPES = ("text", "number", "select", "date", "checkbox")  # the ones add_field can check


class ELNError(Exception):
    """Raised when the ELN refuses something or a check in this package fails."""


def _guard_connection(rest_client, url: str) -> None:
    """Give every request a timeout, and turn network failures into one clear ELNError.

    Done here, on the official package's single sending function, so that every call is
    covered, including ones made directly through `eln.client`.
    """
    # No automatic retries: they'd re-wait for a dead server and print warnings on the user's screen
    rest_client.pool_manager.connection_pool_kw["retries"] = urllib3.Retry(total=None, connect=0, read=0, redirect=3, other=0)
    unreachable = f"Couldn't reach the ELN at {url}. Check the address and your network connection."
    send = rest_client.request

    def request(*args, **kwargs):
        kwargs["_request_timeout"] = kwargs.get("_request_timeout") or TIMEOUT
        try:
            return send(*args, **kwargs)
        except urllib3.exceptions.HTTPError as e:
            if isinstance(getattr(e, "reason", e), urllib3.exceptions.ReadTimeoutError):
                raise ELNError(f"The ELN at {url} didn't answer in time.") from None
            raise ELNError(unreachable) from None
        except ApiException as e:
            if e.status == 0:  # how the official package reports a failed secure connection
                raise ELNError(unreachable) from None
            raise

    rest_client.request = request


def _server_message(e: ApiException) -> str:
    """The reason eLabFTW gave for refusing a request, e.g. 'Metadata field Scan Rate expects a number.'"""
    try:
        return json.loads(e.body)["message"]
    except (ValueError, KeyError, TypeError):
        return f"{e.status} {e.reason}"


@dataclass
class Template:
    """An experiment template: its ID, title and fields, in the template's order.

    You get one from `ELN.get_template`. You don't create it yourself.

    Attributes:
        id: The template's ID in eLabFTW.
        title: The template's title.
        fields: Field name → its settings, in the order shown on the web page.
            For example `{"Scan Rate": {"type": "number", "unit": "deg/min", "value": ""}}`.
    """
    id: int
    title: str
    fields: dict


@dataclass
class Resource:
    """A resource (an entry in the Resources database: a chemical, sample, instrument…).

    You get these from `ELN.find_resources`.

    Attributes:
        id: The resource's ID. Pass it to `ELN.link_resource`.
        title: The resource's title.
        category: Its category, e.g. "Chemical Compound" or "Instrument".
    """
    id: int
    title: str
    category: str


@dataclass
class ExperimentSummary:
    """One experiment in a search result: enough to tell whether it's the one you want.

    You get these from `ELN.find_experiments`.

    Attributes:
        id: The experiment's ID. Pass it to `ELN.link_experiment`, `ELN.add_note` and so on.
        title: The experiment's title.
        date: The experiment's date, e.g. `"2026-10-28"`.
        owner: The full name of the person who owns it.
        status: Its status, e.g. `"Success"`. Empty if it has none.
        template_id: The template it was created from. None if it wasn't created from one,
            which is the case for most experiments made before this package.
        tags: Its tags.
        fields: Field name → value, in the order shown on the web page.
        first_line: The first line of the main text, up to 100 characters.
    """
    id: int
    title: str
    date: str
    owner: str
    status: str
    template_id: int | None
    tags: list[str]
    fields: dict
    first_line: str


@dataclass
class Attachment:
    """A file attached to an experiment.

    Attributes:
        id: The upload's ID.
        name: The file's name.
        comment: The note shown next to the file. Empty if it has none.
        size: The file's size in bytes.
    """
    id: int
    name: str
    comment: str
    size: int


@dataclass
class Experiment(ExperimentSummary):
    """One whole experiment: everything in `ExperimentSummary`, plus its text and files.

    You get one from `ELN.get_experiment`.

    Attributes:
        main_text: The main text as stored: HTML, or Markdown if the experiment uses Markdown.
        files: The files attached to it.
    """
    main_text: str
    files: list[Attachment]


@dataclass
class LinkedExperiment:
    """An experiment at the other end of a link.

    Attributes:
        id: The experiment's ID.
        title: The experiment's title.
    """
    id: int
    title: str


@dataclass
class Links:
    """What an experiment is linked to. You get this from `ELN.get_links`.

    Attributes:
        resources: The resources this experiment links to.
        experiments: The experiments this experiment links to.
        linked_from: Other experiments that link to this one.
    """
    resources: list[Resource]
    experiments: list[LinkedExperiment]
    linked_from: list[LinkedExperiment]


class ELN:
    """A connection to the lab's eLabFTW, acting as the owner of the API key.

    Make one at the start of your script and use it for everything else.
    It checks the key right away, so a wrong key fails here, not halfway through a run.

    Every function here raises `ELNError` if the ELN can't be reached or stops answering.

    Args:
        url: Address of the ELN. If not given: the `ELN_URL` environment
            variable, else https://eln.ddomlab.org.
        api_key: Your eLabFTW API key. If not given: the `ELN_API_KEY`
            environment variable. Never write the key into a script.

    Raises:
        ELNError: If no key is found, the ELN doesn't accept it, or the ELN can't be reached.

    Example:
        >>> from ddomlab_eln import ELN
        >>> eln = ELN()
        >>> eln
        <ELN https://eln.ddomlab.org as Ada Lovelace>
    """

    def __init__(self, url: str | None = None, api_key: str | None = None):
        # 1. Where and who: use the arguments if given, else environment variables
        self.url = (url or os.environ.get("ELN_URL") or DEFAULT_URL).rstrip("/")
        api_key = api_key or os.environ.get("ELN_API_KEY")
        if not api_key:
            raise ELNError(
                "No API key found. Set the ELN_API_KEY environment variable "
                "to the key from your eLabFTW user settings."
            )

        # 2. Set up the official package. eln.client is the escape hatch for anything we don't wrap.
        config = elabapi_python.Configuration()
        config.host = self.url + "/api/v2"
        self.client = elabapi_python.ApiClient(config)
        self.client.set_default_header(header_name="Authorization", header_value=api_key)
        _guard_connection(self.client.rest_client, self.url)

        # 3. Check the connection now, so a bad key fails here with a clear message
        try:
            self.user = elabapi_python.UsersApi(self.client).read_user("me")
        except ApiException as e:
            if e.status == 401:
                raise ELNError(f"The ELN at {self.url} doesn't accept this API key.") from None
            raise ELNError(f"Couldn't connect to {self.url}: {e.status} {e.reason}") from None

    def __repr__(self):
        return f"<ELN {self.url} as {self.user.fullname}>"

    def get_template(self, template_id: int) -> Template:
        """Read one experiment template and its fields.

        Args:
            template_id: The template's ID: the number after `id=` in its web address.

        Returns:
            The template, with its fields in the order shown on the web page.

        Raises:
            ELNError: If there is no such template, or you can't see it.

        Example:
            >>> t = eln.get_template(18)
            >>> t.title
            'TEST XRD run'
            >>> list(t.fields)
            ['Sample ID', 'Scan Rate', 'Mode']
        """
        api = elabapi_python.ExperimentsTemplatesApi(self.client)
        try:
            response = api.get_experiment_template(template_id, _preload_content=False)
        except ApiException as e:
            if e.status in (403, 404):
                raise ELNError(f"There is no experiment template #{template_id} that you can see.") from None
            raise ELNError(f"Couldn't read template #{template_id}: {e.status} {e.reason}") from None

        data = json.loads(response.data)
        return Template(id=data["id"], title=data["title"], fields=_fields_in_order(data["metadata"]))

    def list_templates(self) -> list[Template]:
        """List the experiment templates you can use, with their fields.

        Returns:
            Every experiment template you're allowed to see.

        Raises:
            ELNError: If the ELN refuses the request.

        Example:
            >>> for t in eln.list_templates():
            ...     print(t.id, t.title, list(t.fields))
            18 TEST XRD run ['Sample ID', 'Scan Rate', 'Mode']
            7 Electrochemistry []
        """
        api = elabapi_python.ExperimentsTemplatesApi(self.client)
        try:
            response = api.read_experiments_templates(full=1, _preload_content=False)
        except ApiException as e:
            raise ELNError(f"Couldn't list the templates: {_server_message(e)}") from None
        return [Template(id=t["id"], title=t["title"], fields=_fields_in_order(t["metadata"]))
                for t in json.loads(response.data)]

    def create_experiment(self, template_id: int, title: str, fields: dict | None = None) -> int:
        """Create a new experiment from a template.

        Before sending anything, checks that every field name exists in the template
        (catching typos) and that every required field has a value.

        Args:
            template_id: The ID of the experiment template to start from.
            title: The new experiment's title.
            fields: Field name → value, e.g. `{"Sample ID": "S-12", "Scan Rate": 2}`.
                Fields you leave out keep the template's value.

        Returns:
            The new experiment's ID. Use it to upload files or add links.

        Raises:
            ELNError: If the template doesn't exist, a field name isn't in the template,
                a required field is empty, or the ELN refuses a value (text in a number
                field, a dropdown value that isn't an option).

        Example:
            >>> exp_id = eln.create_experiment(18, "XRD scan S-12", {"Sample ID": "S-12", "Scan Rate": 2})
            >>> exp_id
            612
        """
        fields = fields or {}
        template = self.get_template(template_id)
        problems = _field_problems(template.fields, fields, f"template #{template_id}", every_required=True)
        if problems:
            raise ELNError("Can't create the experiment:\n  - " + "\n  - ".join(problems))

        values = {name: {"value": value} for name, value in fields.items()}
        body = {
            "template": template_id,
            "title": title,
            "metadata": json.dumps({"extra_fields": values}),
        }
        api = elabapi_python.ExperimentsApi(self.client)
        try:
            _, _, headers = api.post_experiment_with_http_info(body=body)
        except ApiException as e:
            raise ELNError(f"The ELN refused to create the experiment: {_server_message(e)}") from None
        return _id_from_location(headers)

    def update_fields(self, exp_id: int, fields: dict) -> None:
        """Change the values of an existing experiment's fields.

        All or nothing: if any value is refused, none are changed. Fields you leave out
        keep their value. Only fields the experiment already has can be changed.

        Args:
            exp_id: The experiment's ID.
            fields: Field name → new value, e.g. `{"Scan Rate": 3, "Mode": "fast"}`.

        Raises:
            ELNError: If there is no such experiment, a field name isn't in it, a required
                field would be empty, or the ELN refuses a value (text in a number field,
                a dropdown value that isn't an option). Nothing is changed in any of these cases.

        Example:
            >>> eln.update_fields(612, {"Scan Rate": 3, "Mode": "fast"})
        """
        exp = self._read_experiment(exp_id)
        known = json.loads(exp["metadata"] or "{}").get("extra_fields", {})
        problems = _field_problems(known, fields, f"experiment #{exp_id}", every_required=False)
        if problems:
            raise ELNError(f"Didn't update experiment #{exp_id}:\n  - " + "\n  - ".join(problems))

        # metadatamerge checks every value first and then saves them together, so a refused
        # value can't leave the others half-saved (updatemetadatafield saves one at a time)
        values = {name: {"value": value} for name, value in fields.items()}
        self._patch_experiment(exp_id, {"metadatamerge": json.dumps({"extra_fields": values})}, "update the fields of")

    def add_field(
        self,
        exp_id: int,
        name: str,
        value="",
        *,
        field_type: str = "text",
        unit: str | None = None,
        options: list[str] | None = None,
    ) -> None:
        """Add a new field to one experiment. Its template isn't changed.

        Use this for a value only some runs have. If every run needs the field, add it
        to the template instead. To change a field that already exists, use `ELN.update_fields`.

        Args:
            exp_id: The experiment's ID.
            name: The new field's name.
            value: Its value. Leave out to add it empty. For a `"date"` field, a date like
                `"2026-11-04"`. For a `"checkbox"` field, True or False.
            field_type: One of `"text"`, `"number"`, `"select"` (a dropdown), `"date"` or `"checkbox"`.
            unit: The unit, for a `"number"` field.
            options: The choices, for a `"select"` field.

        Raises:
            ELNError: If the experiment already has a field with this name, there is no such
                experiment, or the type, unit, options or value aren't valid. Nothing is
                changed in any of these cases.

        Example:
            >>> eln.add_field(612, "Room temperature", 21.5, field_type="number", unit="C")
            >>> eln.add_field(612, "Detector", "fast", field_type="select", options=["fast", "slow"])
        """
        name = name.strip()
        if not name:
            raise ELNError("A field needs a name.")
        if field_type not in FIELD_TYPES:
            raise ELNError(f"field_type must be one of {', '.join(FIELD_TYPES)}, not {field_type!r}.")
        if unit and field_type != "number":
            raise ELNError(f'Only a "number" field can have a unit, not a "{field_type}" field.')
        if field_type == "select" and not options:
            raise ELNError('A "select" field needs options.')
        if options and field_type != "select":
            raise ELNError(f'Only a "select" field can have options, not a "{field_type}" field.')

        if field_type == "date" and value:
            value = _day(value, f'The value of "{name}"')
        if field_type == "checkbox":
            if value != "" and not isinstance(value, bool):
                raise ELNError(f'The value of checkbox "{name}" must be True or False, not {value!r}.')
            value = "on" if value is True else ""

        exp = self._read_experiment(exp_id)
        known = _fields_in_order(exp["metadata"])
        same_name = [existing for existing in known if existing.casefold() == name.casefold()]
        if same_name:
            raise ELNError(f'Experiment #{exp_id} already has a field "{same_name[0]}". To change its value, use update_fields.')

        field = {"type": field_type, "value": value}
        if unit:
            field |= {"unit": unit, "units": [unit]}
        if options:
            field["options"] = _as_list(options)
        positions = [int(settings["position"]) for settings in known.values() if "position" in settings]
        if positions:
            # The web page can't order a mix of fields with and without a position
            field["position"] = max(positions) + 1
        # metadatamerge checks numbers and options, and leaves the other fields alone
        self._patch_experiment(exp_id, {"metadatamerge": json.dumps({"extra_fields": {name: field}})}, "add a field to")

    def add_tags(self, exp_id: int, tags: str | list[str]) -> None:
        """Add one or more tags to an experiment.

        A tag it already has is harmless. If the lab already has the tag in different
        upper or lower case, the existing spelling is used (`p3ht` becomes `P3HT`).

        Args:
            exp_id: The experiment's ID.
            tags: A tag, or a list of tags.

        Raises:
            ELNError: If a tag is empty, there is no such experiment, or the ELN refuses a
                tag. Tags added before a refused one stay added.

        Example:
            >>> eln.add_tags(612, ["XRD", "P3HT"])
        """
        tags = _as_list(tags)
        if not all(str(tag).strip() for tag in tags):
            raise ELNError("A tag can't be empty.")
        self._read_experiment(exp_id)
        api = elabapi_python.TagsApi(self.client)
        for tag in tags:
            try:
                api.post_tag("experiments", exp_id, body={"tag": str(tag)})
            except ApiException as e:
                raise ELNError(f'The ELN refused to add the tag "{tag}" to experiment #{exp_id}: {_server_message(e)}') from None

    def add_note(self, exp_id: int, text: str) -> None:
        """Add text to the end of an experiment's main text.

        Nothing already there is changed. Write plain text: line breaks are kept, and
        characters like `<` and `&` show as typed.

        Args:
            exp_id: The experiment's ID.
            text: The note to add, as plain text.

        Raises:
            ELNError: If there is no such experiment, or you can't edit it.

        Example:
            >>> eln.add_note(612, "Run finished at 14:02. Peak at 2θ = 28.4°")
        """
        exp = self._read_experiment(exp_id)
        if exp["content_type"] == MARKDOWN:
            addition = "\n\n" + text
        else:
            addition = _as_html(text)
        self._patch_experiment(exp_id, {"bodyappend": addition}, "add a note to")

    def set_text(self, exp_id: int, text: str, overwrite: bool = False) -> None:
        """Replace an experiment's whole main text.

        To protect text people typed by hand, this refuses if the experiment already
        has main text, unless you pass `overwrite=True`. eLabFTW does not always save
        a revision, so replaced text may be gone for good. To add text instead, use
        `ELN.add_note`.

        Args:
            exp_id: The experiment's ID.
            text: The new main text, as plain text.
            overwrite: Replace existing main text. Leave False unless the script owns this text.

        Raises:
            ELNError: If there is no such experiment, you can't edit it, or it already
                has main text and `overwrite` is False.

        Example:
            >>> eln.set_text(612, "Summary: 3 scans, all passed.")
            >>> eln.set_text(612, "Summary: 4 scans, all passed.", overwrite=True)
        """
        exp = self._read_experiment(exp_id)
        if (exp["body"] or "").strip() and not overwrite:
            raise ELNError(
                f"Experiment #{exp_id} already has main text. Pass overwrite=True to replace it; "
                "the old text may not be recoverable. To add to it, use add_note."
            )
        new_text = text if exp["content_type"] == MARKDOWN else _as_html(text)
        self._patch_experiment(exp_id, {"body": new_text}, "change the main text of")

    def upload_file(self, exp_id: int, path: str | Path, comment: str | None = None) -> int:
        """Attach a file to an experiment.

        If the same file (same name and same contents) is already attached, nothing is
        uploaded again and the existing upload's ID is returned. So re-running a script
        doesn't pile up copies. A changed file with the same name is uploaded as a new file.

        Args:
            exp_id: The experiment's ID.
            path: The file on this computer, e.g. `"C:/XRD/run43.xy"` or a `pathlib.Path`.
            comment: A short note shown next to the file on the web page.

        Returns:
            The upload's ID.

        Raises:
            ELNError: If the file doesn't exist, there is no such experiment, you can't
                edit it, or the file is bigger than the ELN accepts (100 MB on the lab's ELN).

        Example:
            >>> eln.upload_file(612, "C:/XRD/run43.xy", comment="raw scan")
            11766
        """
        path = Path(path)
        if not path.is_file():
            raise ELNError(f"There is no file at {path}.")
        with path.open("rb") as f:
            fingerprint = hashlib.file_digest(f, "sha256").hexdigest()

        for upload in self._read_experiment(exp_id)["uploads"] or []:
            if upload["real_name"] == path.name and upload["hash"] == fingerprint:
                return upload["id"]

        extra = {"comment": comment} if comment else {}
        api = elabapi_python.UploadsApi(self.client)
        try:
            _, _, headers = api.post_upload_with_http_info("experiments", exp_id, file=str(path), **extra)
        except ApiException as e:
            if e.status == 413:
                size_mb = path.stat().st_size / 1024**2  # MB the way the server's "100M" limit counts
                raise ELNError(f"{path.name} ({size_mb:.0f} MB) is bigger than the ELN accepts.") from None
            raise ELNError(f"The ELN refused to upload {path.name} to experiment #{exp_id}: {_server_message(e)}") from None
        return _id_from_location(headers)

    def find_resources(self, text: str) -> list[Resource]:
        """Search resources by title (and text), to find the ID to link.

        Returns every match; it never picks one for you. The lab has several resources
        called "acetone", so check the title and category and use the right ID.

        Args:
            text: What to search for. Upper or lower case doesn't matter.

        Returns:
            Matching resources, those whose title matches best first. Empty if none match.

        Example:
            >>> for r in eln.find_resources("acetone"):
            ...     print(r.id, r.title, r.category)
            562 acetone Chemical Compound
            279 Acetone Chemical Compound
            577 Acetone (spectrophotometric grade) Chemical Compound
        """
        api = elabapi_python.ItemsApi(self.client)
        try:
            # scope=3: everything you're allowed to see, whatever your web-page settings say
            response = api.read_items(q=text, limit=100, scope=3, _preload_content=False)
        except ApiException as e:
            raise ELNError(f"Couldn't search resources: {_server_message(e)}") from None
        found = [Resource(id=r["id"], title=r["title"], category=r["category_title"] or "")
                 for r in json.loads(response.data)]

        # exact title first, then titles that contain the text, then matches only in the main text
        wanted = text.casefold()
        return sorted(found, key=lambda r: (r.title.casefold() != wanted, wanted not in r.title.casefold()))

    def link_resource(self, exp_id: int, resource_id: int) -> str:
        """Link a resource to an experiment (it shows under "Linked resources").

        Linking the same resource twice is harmless: there is still one link.

        Args:
            exp_id: The experiment's ID.
            resource_id: The resource's ID, e.g. from `ELN.find_resources`.

        Returns:
            The resource's title, so your script can log what it linked.

        Raises:
            ELNError: If the experiment or the resource doesn't exist or you can't see it,
                or you can't edit the experiment.

        Example:
            >>> eln.link_resource(612, 562)
            'acetone'
        """
        self._read_experiment(exp_id)
        resource = self._read_item(resource_id)
        api = elabapi_python.LinksToItemsApi(self.client)
        try:
            api.post_entity_items_links("experiments", exp_id, resource_id)
        except ApiException as e:
            raise ELNError(f"The ELN refused to link resource #{resource_id} to experiment #{exp_id}: {_server_message(e)}") from None
        return resource["title"]

    def link_experiment(self, exp_id: int, other_id: int) -> str:
        """Link another experiment to this one (it shows under "Linked experiments").

        The other experiment then lists this one as related. Linking twice is harmless.

        Args:
            exp_id: The experiment to add the link to.
            other_id: The experiment to link to. Must be a different experiment.

        Returns:
            The other experiment's title, so your script can log what it linked.

        Raises:
            ELNError: If the two IDs are the same, either experiment doesn't exist or you
                can't see it, or you can't edit the first one.

        Example:
            >>> eln.link_experiment(612, 598)
            'XRD scan S-11'
        """
        if exp_id == other_id:
            raise ELNError(f"Can't link experiment #{exp_id} to itself.")
        self._read_experiment(exp_id)
        other = self._read_experiment(other_id)
        api = elabapi_python.LinksToExperimentsApi(self.client)
        try:
            api.post_entity_experiments_links("experiments", exp_id, other_id)
        except ApiException as e:
            raise ELNError(f"The ELN refused to link experiment #{other_id} to experiment #{exp_id}: {_server_message(e)}") from None
        return other["title"]

    def get_links(self, exp_id: int) -> Links:
        """See what an experiment is linked to, and what links to it.

        Links to anything in the ELN's trash are left out.

        Args:
            exp_id: The experiment's ID.

        Returns:
            Its linked resources and experiments, and the experiments linking to it.

        Raises:
            ELNError: If there is no such experiment, or you can't see it.

        Example:
            >>> links = eln.get_links(612)
            >>> [r.title for r in links.resources]
            ['acetone']
        """
        exp = self._read_experiment(exp_id)
        return Links(
            resources=[Resource(id=link["entityid"], title=link["title"], category=link["category_title"] or "")
                       for link in exp["items_links"] or []],
            experiments=[LinkedExperiment(id=link["entityid"], title=link["title"]) for link in exp["experiments_links"] or []],
            linked_from=[LinkedExperiment(id=link["entityid"], title=link["title"]) for link in exp["related_experiments_links"] or []],
        )

    def get_experiment(self, exp_id: int) -> Experiment:
        """Read one experiment: its fields, tags, main text and files.

        Args:
            exp_id: The experiment's ID.

        Returns:
            The experiment. Number fields come back as real numbers.

        Raises:
            ELNError: If there is no such experiment, or you can't see it.

        Example:
            >>> exp = eln.get_experiment(612)
            >>> exp.fields["Scan Rate"]
            2
            >>> [f.name for f in exp.files]
            ['run43.xy']
        """
        row = self._read_experiment(exp_id)
        files = [Attachment(id=u["id"], name=u["real_name"], comment=u["comment"] or "", size=u["filesize"])
                 for u in row["uploads"] or []]
        return Experiment(**vars(_summary(row)), main_text=row["body"] or "", files=files)

    def find_experiments(
        self,
        text: str | None = None,
        *,
        fields: dict | None = None,
        since: str | date | None = None,
        until: str | date | None = None,
        template_id: int | None = None,
        tags: str | list[str] | None = None,
        status: str | None = None,
        owner: int | str | None = None,
        limit: int = 20,
    ) -> list[ExperimentSummary]:
        """Search experiments, newest first. With no options: your 20 newest.

        Options can be combined, and an experiment must match all of them. To print the
        results as a table, pass them to `show_experiments`.

        Args:
            text: Words to find in the title or main text. Every word must be there, in any order.
            fields: Field name → exact value, e.g. `{"Sample ID": "S-12"}` (upper or lower case
                doesn't matter). Use `%` for "anything": `"S-%"` finds S-1 and S-12.
            since: Only experiments dated on or after this day, e.g. `"2026-10-01"`.
            until: Only experiments dated on or before this day.
            template_id: Only experiments created from this template. Most experiments made
                before this package don't record a template, so they won't be found this way.
            tags: Only experiments that have this tag, or all of these tags.
            status: Only experiments with exactly this status, e.g. `"Success"`.
            owner: Whose experiments: leave out for your own, give a user ID for a colleague's,
                or `"all"` for everyone's you're allowed to see.
            limit: The most results to return.

        Returns:
            The matching experiments, newest first. Empty if none match.

        Raises:
            ELNError: If `since`, `until`, `owner` or `limit` isn't valid, or the search fails.

        Example:
            >>> runs = eln.find_experiments("P3HT", since="2026-10-01")
            >>> show_experiments(runs)
            >>> eln.link_experiment(new_id, runs[0].id)
        """
        if limit < 1:
            raise ELNError("limit must be 1 or more.")
        if owner is not None and owner != "all" and not isinstance(owner, int):
            raise ELNError(f'owner must be a user ID or "all", not {owner!r}.')

        # scope=3 so your web-page settings don't change the results. "Only mine" has to be
        # owner=<id>: the ELN ignores scope=1 as soon as there's an extended search.
        search = {"scope": 3, "full": 1, "order": "id", "sort": "desc"}
        if owner != "all":
            search["owner"] = str(self.user.userid if owner is None else owner)
        if text and text.split():
            # Quoted, because the ELN reads this as search syntax: a:b would match everything
            search["q"] = " ".join(_quoted(word) for word in text.split())
        if tags:
            search["tags"] = _as_list(tags)

        fields = fields or {}
        extended = [f"extrafield:{_quoted(name)}:{_quoted(value)}" for name, value in fields.items()]
        if since:
            extended.append(f"date:>={_day(since, 'since')}")
        if until:
            extended.append(f"date:<={_day(until, 'until')}")
        if status:
            extended.append(f"status:{_quoted(status)}")
        if extended:
            search["extended"] = " AND ".join(extended)

        def wanted(exp: ExperimentSummary) -> bool:
            # The ELN only offers partial matches for fields and status ("S-1" also finds S-12)
            return (
                all(_same_value(exp.fields.get(name), value) for name, value in fields.items())
                and (not status or exp.status.casefold() == status.casefold())
                and (template_id is None or exp.template_id == template_id)
            )

        # When we filter here too, a page can come back with few or no keepers, so keep reading
        page_size = 100 if fields or status or template_id is not None else min(limit, 100)
        api = elabapi_python.ExperimentsApi(self.client)
        found = []
        offset = 0
        while len(found) < limit:
            try:
                response = api.read_experiments(limit=page_size, offset=offset, _preload_content=False, **search)
            except ApiException as e:
                raise ELNError(f"Couldn't search experiments: {_server_message(e)}") from None
            rows = json.loads(response.data)
            found += [exp for exp in map(_summary, rows) if wanted(exp)]
            if len(rows) < page_size:
                break
            offset += page_size

        # The ELN lists pinned experiments first whatever the sort order
        return sorted(found, key=lambda exp: exp.id, reverse=True)[:limit]

    def _read_item(self, item_id: int) -> dict:
        """A resource exactly as the ELN sends it (raw JSON)."""
        api = elabapi_python.ItemsApi(self.client)
        try:
            response = api.get_item(item_id, _preload_content=False)
        except ApiException as e:
            if e.status in (403, 404):
                raise ELNError(f"There is no resource #{item_id} that you can see.") from None
            raise ELNError(f"Couldn't read resource #{item_id}: {_server_message(e)}") from None
        return json.loads(response.data)

    def _read_experiment(self, exp_id: int) -> dict:
        """The experiment exactly as the ELN sends it (raw JSON, so nothing gets lost)."""
        api = elabapi_python.ExperimentsApi(self.client)
        try:
            response = api.get_experiment(exp_id, _preload_content=False)
        except ApiException as e:
            if e.status in (403, 404):
                raise ELNError(f"There is no experiment #{exp_id} that you can see.") from None
            raise ELNError(f"Couldn't read experiment #{exp_id}: {_server_message(e)}") from None
        return json.loads(response.data)

    def _patch_experiment(self, exp_id: int, changes: dict, doing: str) -> None:
        """Send one change to an experiment. `doing` completes the error message, e.g. "add a note to"."""
        api = elabapi_python.ExperimentsApi(self.client)
        try:
            api.patch_experiment(id=exp_id, body=changes)
        except ApiException as e:
            raise ELNError(f"The ELN refused to {doing} experiment #{exp_id}: {_server_message(e)}") from None


def show_experiments(experiments: list[ExperimentSummary]) -> None:
    """Print search results as a table, for choosing one by eye.

    Args:
        experiments: The results of `ELN.find_experiments`.

    Example:
        >>> show_experiments(eln.find_experiments("XRD"))
        ID   Date        Owner      Status   Title          Fields
        613  2026-10-28  Ada Byron  Success  XRD scan S-12  Sample ID=S-12, Scan Rate=2
    """
    if not experiments:
        print("No experiments found.")
        return

    def short(text: str, length: int) -> str:
        return text if len(text) <= length else text[:length - 1] + "…"

    rows = [("ID", "Date", "Owner", "Status", "Title", "Fields")]
    for exp in experiments:
        first_fields = ", ".join(f"{name}={value}" for name, value in list(exp.fields.items())[:2])
        rows.append((str(exp.id), exp.date, short(exp.owner, 20), exp.status, short(exp.title, 40), short(first_fields, 40)))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _fields_in_order(metadata: str | None) -> dict:
    """A template's or experiment's fields from its raw metadata, in the order the web page shows."""
    extra_fields = json.loads(metadata or "{}").get("extra_fields", {})
    return dict(sorted(extra_fields.items(), key=lambda item: item[1].get("position", 999)))


def _summary(row: dict) -> ExperimentSummary:
    """One experiment from a search (raw JSON, requested with full=1) as an ExperimentSummary."""
    return ExperimentSummary(
        id=row["id"],
        title=row["title"],
        date=row["date"] or "",
        owner=row["fullname"],
        status=row["status_title"] or "",
        template_id=row["created_from_id"] if row["created_from_type"] == FROM_TEMPLATE else None,
        tags=[tag["tag"] for tag in row["tags_decoded"] or []],
        fields={name: _field_value(settings) for name, settings in _fields_in_order(row["metadata"]).items()},
        first_line=_first_line(row["body"]),
    )


def _field_value(settings: dict):
    """A field's value, with number fields as real numbers (values sent at creation are stored as text)."""
    value = settings.get("value")
    if settings.get("type") == "number" and isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def _first_line(body: str | None) -> str:
    """The first line of an experiment's main text, without HTML, up to 100 characters."""
    text = re.sub(r"</(p|div|li|tr|h[1-6])>|<br\s*/?>", "\n", body or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0][:100] if lines else ""


def _same_value(stored, wanted) -> bool:
    """Whether a field's value is `wanted`: whole value, any case, with % meaning "anything"."""
    if isinstance(stored, list):
        return any(_same_value(item, wanted) for item in stored)
    if stored is None:
        return False
    pattern = ".*".join(re.escape(part) for part in str(wanted).split("%"))
    return re.fullmatch(pattern, str(stored), re.IGNORECASE | re.DOTALL) is not None


def _as_list(value) -> list:
    """One value or several, as a list. A lone string stays whole: list("P3HT") would split its letters."""
    return [value] if isinstance(value, str) else list(value)


def _quoted(value) -> str:
    """A value as a quoted term for the ELN's search syntax."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _day(value: str | date, option: str) -> str:
    """A date option as YYYY-MM-DD, or a clear error if it isn't one."""
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ELNError(f'{option} must be a date like "2026-10-01", not {value!r}.') from None


def _id_from_location(headers) -> int:
    """The ID of what was just created. The server says where it is, e.g. .../experiments/612/uploads/11766."""
    return int(headers["Location"].rstrip("/").split("/")[-1])


def _as_html(text: str) -> str:
    """Plain text → one HTML paragraph, keeping line breaks and showing < and & as typed."""
    return "<p>" + "<br>".join(html.escape(line) for line in text.splitlines()) + "</p>"


def _field_problems(known: dict, fields: dict, source: str, every_required: bool) -> list[str]:
    """What's wrong with `fields` before anything is sent: misspelled names and empty required fields.

    `known` is the template's or experiment's fields. With `every_required`, all required fields
    must end up with a value (when creating); without it, only the ones being set (when updating).
    """
    problems = []
    for name in fields:
        if name not in known:
            close = difflib.get_close_matches(name, known, n=1)
            hint = f' Did you mean "{close[0]}"?' if close else ""
            problems.append(f'"{name}" is not a field of {source}.{hint}')
    for name, settings in known.items():
        if not every_required and name not in fields:
            continue
        value = fields.get(name, settings.get("value"))
        if settings.get("required") and value in (None, ""):
            problems.append(f'"{name}" is required.')
    return problems
