"""The connection to the DDOM Lab eLabFTW."""
import difflib
import hashlib
import html
import json
import os
from dataclasses import dataclass
from pathlib import Path

import elabapi_python
from elabapi_python.rest import ApiException

DEFAULT_URL = "https://eln.ddomlab.org"
MARKDOWN = 2  # an experiment's content_type: 1 = rich-text editor (HTML), 2 = Markdown


class ELNError(Exception):
    """Raised when the ELN refuses something or a check in this package fails."""


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


class ELN:
    """A connection to the lab's eLabFTW, acting as the owner of the API key.

    Make one at the start of your script and use it for everything else.
    It checks the key right away, so a wrong key fails here, not halfway through a run.

    Args:
        url: Address of the ELN. If not given: the `ELN_URL` environment
            variable, else https://eln.ddomlab.org.
        api_key: Your eLabFTW API key. If not given: the `ELN_API_KEY`
            environment variable. Never write the key into a script.

    Raises:
        ELNError: If no key is found, or the ELN doesn't accept it.

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
        extra_fields = json.loads(data["metadata"] or "{}").get("extra_fields", {})
        in_order = dict(sorted(extra_fields.items(), key=lambda item: item[1].get("position", 999)))
        return Template(id=data["id"], title=data["title"], fields=in_order)

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
