import itertools
from copy import deepcopy
from io import BytesIO
import re
from typing import Union, Callable, Any, Tuple, List
from datetime import datetime

import requests
from lxml import etree

from . import exceptions

__all__ = ["Document"]

DEFAULT_XMLPARSER = etree.XMLParser(
    remove_blank_text=False,
    remove_comments=False,
    load_dtd=False,
    no_network=True,
    collect_ids=False,
)

SUBJECT_AREAS = (
    "AGRICULTURAL SCIENCES",
    "APPLIED SOCIAL SCIENCES",
    "BIOLOGICAL SCIENCES",
    "ENGINEERING",
    "EXACT AND EARTH SCIENCES",
    "HEALTH SCIENCES",
    "HUMAN SCIENCES",
    "LINGUISTIC, LITERATURE AND ARTS",
)


def utcnow():
    return str(datetime.utcnow().isoformat() + "Z")


class DocumentManifest:
    """Namespace para funções que manipulam o manifesto do documento.
    """

    @staticmethod
    def new(id: str) -> dict:
        return {"id": str(id), "versions": []}

    def _new_version(
        data_uri: str, assets: Union[dict, list], now: Callable[[], str]
    ) -> dict:
        _assets = {str(aid): [] for aid in assets}
        return {"data": data_uri, "assets": _assets, "timestamp": now()}

    @staticmethod
    def add_version(
        manifest: dict,
        data_uri: str,
        assets: Union[dict, list],
        now: Callable[[], str] = utcnow,
    ) -> dict:
        _manifest = deepcopy(manifest)
        version = DocumentManifest._new_version(data_uri, assets, now=now)
        for asset_id in assets:
            try:
                asset_uri = assets[asset_id]
            except TypeError:
                break
            else:
                if asset_uri:
                    version = DocumentManifest._new_asset_version(
                        version, asset_id, asset_uri, now=now
                    )
        _manifest["versions"].append(version)
        return _manifest

    def _new_asset_version(
        version: dict, asset_id: str, asset_uri: str, now: Callable[[], str] = utcnow
    ) -> dict:
        _version = deepcopy(version)
        _version["assets"][asset_id].append((now(), asset_uri))
        return _version

    @staticmethod
    def add_asset_version(
        manifest: dict, asset_id: str, asset_uri: str, now: Callable[[], str] = utcnow
    ) -> dict:
        _manifest = deepcopy(manifest)
        _manifest["versions"][-1] = DocumentManifest._new_asset_version(
            _manifest["versions"][-1], asset_id, asset_uri, now=now
        )
        return _manifest


def get_static_assets(xml_et):
    """Retorna uma lista das URIs dos ativos digitais de ``xml_et``.
    """
    paths = [
        "//graphic[@xlink:href]",
        "//media[@xlink:href]",
        "//inline-graphic[@xlink:href]",
        "//supplementary-material[@xlink:href]",
        "//inline-supplementary-material[@xlink:href]",
    ]

    iterators = [
        xml_et.iterfind(path, namespaces={"xlink": "http://www.w3.org/1999/xlink"})
        for path in paths
    ]

    elements = itertools.chain(*iterators)

    return [
        (element.attrib["{http://www.w3.org/1999/xlink}href"], element)
        for element in elements
    ]


def fetch_data(url: str, timeout: float = 2) -> bytes:
    try:
        response = requests.get(url, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise exceptions.RetryableError(exc) from exc
    except (requests.InvalidSchema, requests.MissingSchema, requests.InvalidURL) as exc:
        raise exceptions.NonRetryableError(exc) from exc
    else:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if 400 <= exc.response.status_code < 500:
                raise exceptions.NonRetryableError(exc) from exc
            elif 500 <= exc.response.status_code < 600:
                raise exceptions.RetryableError(exc) from exc
            else:
                raise

    return response.content


def assets_from_remote_xml(
    url: str, timeout: float = 2, parser=DEFAULT_XMLPARSER
) -> list:
    data = fetch_data(url, timeout)
    xml = etree.parse(BytesIO(data), parser)
    return xml, get_static_assets(xml)


class Document:
    _timestamp_pattern = (
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2})?Z)?$"
    )

    def __init__(self, id=None, manifest=None):
        assert any([id, manifest])
        self.manifest = manifest or DocumentManifest.new(id)

    @property
    def manifest(self):
        return deepcopy(self._manifest)

    @manifest.setter
    def manifest(self, value):
        self._manifest = value

    def id(self):
        return self.manifest.get("id", "")

    def new_version(
        self, data_url, assets_getter=assets_from_remote_xml, timeout=2
    ) -> None:
        """Adiciona `data_url` como uma nova versão do documento.

        :param data_url: é a URL para a nova versão do documento.
        :param assets_getter: (optional) função que recebe 2 argumentos: 1)
        a URL do XML do documento e 2) o timeout para a requisição e retorna
        o par ``(xml, [(href, xml_node), ...]`` onde ``xml`` é uma instância
        de *element tree* da *lxml* e ``[(href, xml_node), ...]`` é uma lista
        que associa as URIs dos ativos com os nós do XML onde se encontram.
        Essa função deve ainda lançar as ``RetryableError`` e
        ``NonRetryableError`` para representar problemas no acesso aos dados
        do XML.
        """
        try:
            latest_version = self.version()
        except ValueError:
            latest_version = {"data": ""}

        if latest_version.get("data") == data_url:
            raise exceptions.VersionAlreadySet(
                "could not add version: the version is equal to the latest one"
            )

        _, data_assets = assets_getter(data_url, timeout=timeout)
        data_assets_keys = [asset_key for asset_key, _ in data_assets]
        assets = self._link_assets(data_assets_keys)
        self.manifest = DocumentManifest.add_version(self._manifest, data_url, assets)

    def _link_assets(self, tolink: list) -> dict:
        """Retorna um mapa entre as chaves dos ativos em `tolink` e as
        referências já existentes na última versão.
        """
        try:
            latest_version = self.version()
        except ValueError:
            latest_version = {"assets": {}}

        return {
            asset_key: latest_version["assets"].get(asset_key, "")
            for asset_key in tolink
        }

    def version(self, index=-1) -> dict:
        try:
            version = self.manifest["versions"][index]
        except IndexError:
            raise ValueError("missing version for index: %s" % index) from None

        def _latest(uris):
            try:
                return uris[-1][1]
            except IndexError:
                return ""

        assets = {a: _latest(u) for a, u in version["assets"].items()}
        version["assets"] = assets
        return version

    def version_at(self, timestamp: str) -> dict:
        """Obtém os metadados da versão no momento `timestamp`.

        :param timestamp: string de texto do timestamp UTC ISO 8601 no formato
        `YYYY-MM-DDTHH:MM:SSSSSSZ`. A resolução pode variar desde o dia, e.g.,
        `2018-09-17`, dia horas e minutos, e.g., `2018-09-17T14:25Z`, ou dia
        horas minutos segundos (e frações em até 6 casas decimais). Caso a
        resolução esteja no nível do dia, a mesma será ajustada automaticamente
        para o nível dos microsegundos por meio da concatenação da string
        `T23:59:59:999999Z` ao valor de `timestamp`.
        """
        if not re.match(self._timestamp_pattern, timestamp):
            raise ValueError(
                "invalid format for timestamp: %s: must match pattern: %s"
                % (timestamp, self._timestamp_pattern)
            )

        if re.match(r"^\d{4}-\d{2}-\d{2}$", timestamp):
            timestamp = f"{timestamp}T23:59:59.999999Z"

        try:
            target_version = max(
                itertools.takewhile(
                    lambda version: version.get("timestamp", "") <= timestamp,
                    self.manifest["versions"],
                ),
                key=lambda version: version.get("timestamp", ""),
            )
        except ValueError:
            raise ValueError("missing version for timestamp: %s" % timestamp) from None

        def _at_time(uris):
            try:
                target = max(
                    itertools.takewhile(lambda asset: asset[0] <= timestamp, uris),
                    key=lambda asset: asset[0],
                )
            except ValueError:
                return ""
            return target[1]

        target_assets = {a: _at_time(u) for a, u in target_version["assets"].items()}
        target_version["assets"] = target_assets
        return target_version

    def data(
        self,
        version_index=-1,
        version_at=None,
        assets_getter=assets_from_remote_xml,
        timeout=2,
    ) -> bytes:
        """Retorna o conteúdo do XML, codificado em UTF-8, já com as
        referências aos ativos digitais correspondendo às da versão solicitada.

        Por meio dos argumentos `version_index` e `version_at` é possível
        explicitar a versão desejada a partir de 2 estratégias distintas:
        `version_index` recebe um valor inteiro referente ao índice da versão
        desejada (pense no acesso a uma lista de versões). Já o argumento
        `version_at` recebe um timestamp UTC, em formato textual, e retorna
        a versão do documento naquele dado momento. Estes argumentos são
        mutuamente exclusivos, e `version_at` anula a presença do outro.

        Note que o argumento `version_at` é muito mais poderoso, uma vez que,
        diferentemente do `version_index`, também recupera o estado desejado
        no nível dos ativos digitais do documento.
        """
        version = (
            self.version_at(version_at) if version_at else self.version(version_index)
        )
        xml_tree, data_assets = assets_getter(version["data"], timeout=timeout)

        version_assets = version["assets"]
        for asset_key, target_node in data_assets:
            version_href = version_assets.get(asset_key, "")
            target_node.attrib["{http://www.w3.org/1999/xlink}href"] = version_href

        return etree.tostring(xml_tree, encoding="utf-8", pretty_print=False)

    def new_asset_version(self, asset_id, data_url) -> None:
        """Adiciona `data_url` como uma nova versão do ativo `asset_id` vinculado
        a versão mais recente do documento. É importante notar que nenhuma validação
        será executada em `data_url`.
        """
        try:
            latest_version = self.version()
        except ValueError:
            latest_version = {"assets": {}}

        if latest_version.get("assets", {}).get(asset_id) == data_url:
            raise exceptions.VersionAlreadySet(
                "could not add version: the version is equal to the latest one"
            )

        try:
            self.manifest = DocumentManifest.add_asset_version(
                self._manifest, asset_id, data_url
            )
        except KeyError:
            raise ValueError(
                'cannot add version for "%s": unknown asset_id' % asset_id
            ) from None


class BundleManifest:
    """Namespace para funções que manipulam maços.
    """

    @staticmethod
    def new(bundle_id: str, now: Callable[[], str] = utcnow) -> dict:
        timestamp = now()
        return {
            "id": str(bundle_id),
            "created": timestamp,
            "updated": timestamp,
            "items": [],
            "metadata": {},
        }

    @staticmethod
    def set_metadata(
        bundle: dict,
        name: str,
        value: Union[dict, str],
        now: Callable[[], str] = utcnow,
    ) -> dict:
        _bundle = deepcopy(bundle)
        _now = now()
        metadata = _bundle["metadata"].setdefault(name, [])
        metadata.append((_now, value))
        _bundle["updated"] = _now
        return _bundle

    @staticmethod
    def get_metadata(bundle: dict, name: str, default="") -> Any:
        try:
            return bundle["metadata"].get(name, [])[-1][1]
        except IndexError:
            return default

    @staticmethod
    def get_metadata_all(bundle: dict, name: str) -> Any:
        return bundle["metadata"].get(name, [])

    @staticmethod
    def add_item(bundle: dict, item: str, now: Callable[[], str] = utcnow) -> dict:
        if item in bundle["items"]:
            raise exceptions.AlreadyExists(
                'cannot add item "%s" in bundle: ' "the item already exists" % item
            )
        _bundle = deepcopy(bundle)
        _bundle["items"].append(item)
        _bundle["updated"] = now()
        return _bundle

    @staticmethod
    def insert_item(
        items_bundle: dict, index: int, item: str, now: Callable[[], str] = utcnow
    ) -> dict:
        if item in items_bundle["items"]:
            raise exceptions.AlreadyExists(
                'cannot insert item "%s" in bundle: ' "the item already exists" % item
            )
        _items_bundle = deepcopy(items_bundle)
        _items_bundle["items"].insert(index, item)
        _items_bundle["updated"] = now()
        return _items_bundle

    @staticmethod
    def remove_item(
        items_bundle: dict, item: str, now: Callable[[], str] = utcnow
    ) -> dict:
        if item not in items_bundle["items"]:
            raise exceptions.DoesNotExist(
                'cannot remove item "%s" from bundle: ' "the item does not exist" % item
            )
        _items_bundle = deepcopy(items_bundle)
        _items_bundle["items"].remove(item)
        _items_bundle["updated"] = now()
        return _items_bundle

    @staticmethod
    def set_component(
        components_bundle: dict, name: str, value: Any, now: Callable[[], str] = utcnow
    ) -> None:
        _components_bundle = deepcopy(components_bundle)
        _components_bundle[name] = value
        _components_bundle["updated"] = now()
        return _components_bundle

    @staticmethod
    def get_component(components_bundle: dict, name: str, default: str = "") -> Any:
        return components_bundle.get(name, default)

    @staticmethod
    def remove_component(components_bundle: dict, name: str) -> dict:
        _components_bundle = deepcopy(components_bundle)
        try:
            del _components_bundle[name]
        except KeyError:
            raise exceptions.DoesNotExist(
                f'cannot remove component "{name}" from bundle: '
                "the component does not exist"
            ) from None
        else:
            return _components_bundle


class DocumentsBundle:
    """
    DocumentsBundle representa um conjunto de documentos agnóstico ao modelo de
    publicação. Exemplos de publicação que são DocumentsBundle: Fascículos fechados
    e abertos, Ahead of Print, Documentos Provisórios, Erratas e Retratações.
    """

    def __init__(self, id: str = None, manifest: dict = None):
        assert any([id, manifest])
        self.manifest = manifest or BundleManifest.new(id)

    def id(self):
        return self.manifest.get("id", "")

    def data(self):
        _manifest = self.manifest
        _manifest["metadata"] = {
            attr: value[-1][-1] for attr, value in _manifest["metadata"].items()
        }
        return _manifest

    @property
    def manifest(self):
        return deepcopy(self._manifest)

    @manifest.setter
    def manifest(self, value: dict):
        self._manifest = value

    @property
    def publication_year(self):
        return BundleManifest.get_metadata(self.manifest, "publication_year")

    @publication_year.setter
    def publication_year(self, value: Union[str, int]):
        _value = str(value)
        if not re.match(r"^\d{4}$", _value):
            raise ValueError(
                "cannot set publication_year with value "
                f'"{_value}": the value is not valid'
            )
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "publication_year", _value
        )

    @property
    def volume(self):
        return BundleManifest.get_metadata(self.manifest, "volume")

    @volume.setter
    def volume(self, value: Union[str, int]):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "volume", _value)

    @property
    def number(self):
        return BundleManifest.get_metadata(self.manifest, "number")

    @number.setter
    def number(self, value: Union[str, int]):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "number", _value)

    @property
    def supplement(self):
        return BundleManifest.get_metadata(self.manifest, "supplement")

    @supplement.setter
    def supplement(self, value: Union[str, int]):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "supplement", _value
        )

    @property
    def titles(self):
        return BundleManifest.get_metadata(self.manifest, "titles", {})

    @titles.setter
    def titles(self, value: dict):
        try:
            _value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set titles with value " '"%s": value must be dict' % value
            ) from None
        self.manifest = BundleManifest.set_metadata(self._manifest, "titles", _value)

    def add_document(self, document: str):
        self.manifest = BundleManifest.add_item(self._manifest, document)

    def insert_document(self, index: int, document: str):
        self.manifest = BundleManifest.insert_item(self._manifest, index, document)

    def remove_document(self, document: str):
        self.manifest = BundleManifest.remove_item(self._manifest, document)

    @property
    def documents(self):
        return self.manifest["items"]


class Journal:
    """
    Journal representa um periodico cientifico que contem um conjunto de documentos
    DocumentsBundle.
    """

    def __init__(self, id: str = None, manifest: dict = None):
        assert any([id, manifest])
        self.manifest = manifest or BundleManifest.new(id)

    def id(self):
        return self.manifest.get("id", "")

    def created(self):
        return self.manifest.get("created", "")

    def updated(self):
        return self.manifest.get("updated", "")

    @property
    def manifest(self):
        return deepcopy(self._manifest)

    @manifest.setter
    def manifest(self, value: dict):
        self._manifest = value

    def data(self):
        """Retorna o manifesto completo de um Journal com os
        metadados em sua última versão"""
        _manifest = self.manifest

        for key, value in _manifest["metadata"].items():
            _manifest["metadata"][key] = value[-1][-1]

        return _manifest

    @property
    def mission(self):
        return BundleManifest.get_metadata(self.manifest, "mission", {})

    @mission.setter
    def mission(self, value: dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set mission with value " '"%s": value must be dict' % value
            ) from None
        self.manifest = BundleManifest.set_metadata(self._manifest, "mission", value)

    @property
    def title(self):
        return BundleManifest.get_metadata(self.manifest, "title")

    @title.setter
    def title(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "title", _value)

    @property
    def title_iso(self):
        return BundleManifest.get_metadata(self.manifest, "title_iso")

    @title_iso.setter
    def title_iso(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "title_iso", _value)

    @property
    def short_title(self):
        return BundleManifest.get_metadata(self.manifest, "short_title")

    @short_title.setter
    def short_title(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "short_title", _value
        )

    @property
    def title_slug(self):
        return BundleManifest.get_metadata(self.manifest, "title_slug")

    @title_slug.setter
    def title_slug(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "title_slug", _value
        )

    @property
    def acronym(self):
        return BundleManifest.get_metadata(self.manifest, "acronym")

    @acronym.setter
    def acronym(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "acronym", _value)

    @property
    def scielo_issn(self):
        return BundleManifest.get_metadata(self.manifest, "scielo_issn")

    @scielo_issn.setter
    def scielo_issn(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "scielo_issn", _value
        )

    @property
    def print_issn(self):
        return BundleManifest.get_metadata(self.manifest, "print_issn")

    @print_issn.setter
    def print_issn(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "print_issn", _value
        )

    @property
    def electronic_issn(self):
        return BundleManifest.get_metadata(self.manifest, "electronic_issn")

    @electronic_issn.setter
    def electronic_issn(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "electronic_issn", _value
        )

    @property
    def status(self):
        return BundleManifest.get_metadata(self.manifest, "status")

    @status.setter
    def status(self, value: dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set status with value " '"%s": value must be dict' % repr(value)
            ) from None
        self.manifest = BundleManifest.set_metadata(self._manifest, "status", value)

    @property
    def subject_areas(self):
        return BundleManifest.get_metadata(self.manifest, "subject_areas")

    @subject_areas.setter
    def subject_areas(self, value: tuple):
        try:
            value = tuple(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set subject_areas with value "
                '"%s": value must be tuple' % repr(value)
            ) from None
        invalid = [item for item in value if item not in SUBJECT_AREAS]
        if invalid:
            raise ValueError(
                "cannot set subject_areas with value %s: " % repr(value)
                + "%s are not valid" % repr(invalid)
            )
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "subject_areas", value
        )

    @property
    def sponsors(self) -> Tuple[dict]:
        return BundleManifest.get_metadata(self.manifest, "sponsors")

    @sponsors.setter
    def sponsors(self, value: Tuple[dict]) -> None:
        try:
            value = tuple([dict(sponsor) for sponsor in value])
        except TypeError:
            raise TypeError("cannot set sponsors this type %s" % repr(value)) from None

        self.manifest = BundleManifest.set_metadata(self._manifest, "sponsors", value)

    @property
    def metrics(self):
        return BundleManifest.get_metadata(self.manifest, "metrics", {})

    @metrics.setter
    def metrics(self, value: dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set metrics with value " '"%s": value must be dict' % value
            ) from None
        self.manifest = BundleManifest.set_metadata(self._manifest, "metrics", value)

    @property
    def subject_categories(self):
        return BundleManifest.get_metadata(self.manifest, "subject_categories")

    @subject_categories.setter
    def subject_categories(self, value: Union[list, tuple]):
        try:
            value = list(value)
        except TypeError:
            raise TypeError(
                "cannot set subject_categories with value "
                '"%s": value must be list like object' % value
            ) from None

        self.manifest = BundleManifest.set_metadata(
            self._manifest, "subject_categories", list(value)
        )

    @property
    def institution_responsible_for(self):
        return BundleManifest.get_metadata(
            self.manifest, "institution_responsible_for", ()
        )

    @institution_responsible_for.setter
    def institution_responsible_for(self, value: tuple):
        try:
            value = tuple(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set institution_responsible_for with value "
                '"%s": value must be tuple' % repr(value)
            ) from None

        self.manifest = BundleManifest.set_metadata(
            self.manifest, "institution_responsible_for", value
        )

    @property
    def online_submission_url(self):
        return BundleManifest.get_metadata(self.manifest, "online_submission_url")

    @online_submission_url.setter
    def online_submission_url(self, value: str):
        _value = str(value)
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "online_submission_url", _value
        )

    @property
    def next_journal(self):
        return BundleManifest.get_metadata(self.manifest, "next_journal", {})

    @next_journal.setter
    def next_journal(self, value: dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set next_journal with value "
                '"%s": value must be dict' % repr(value)
            ) from None
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "next_journal", value
        )

    @property
    def logo_url(self):
        return BundleManifest.get_metadata(self.manifest, "logo_url")

    @logo_url.setter
    def logo_url(self, value: str):
        value = str(value)
        self.manifest = BundleManifest.set_metadata(self._manifest, "logo_url", value)

    @property
    def previous_journal(self):
        return BundleManifest.get_metadata(self.manifest, "previous_journal", {})

    @previous_journal.setter
    def previous_journal(self, value: dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            raise TypeError(
                "cannot set previous_journal with value "
                '"%s": value must be dict' % repr(value)
            ) from None
        self.manifest = BundleManifest.set_metadata(
            self._manifest, "previous_journal", value
        )

    @property
    def status_history(self):
        return BundleManifest.get_metadata_all(self.manifest, "status")

    @property
    def contact(self) -> dict:
        return BundleManifest.get_metadata(self.manifest, "contact", {})

    @contact.setter
    def contact(self, value: dict) -> None:
        try:
            value = dict(value)
        except (TypeError, ValueError) as ex:
            raise type(ex)(
                "cannot set contact with value "
                "%s"
                ": value must be dict" % repr(value)
            ) from None

        self.manifest = BundleManifest.set_metadata(self._manifest, "contact", value)

    def add_issue(self, issue: str) -> None:
        self.manifest = BundleManifest.add_item(self._manifest, issue)

    def insert_issue(self, index: int, issue: str) -> None:
        self.manifest = BundleManifest.insert_item(self._manifest, index, issue)

    def remove_issue(self, issue: str) -> None:
        self.manifest = BundleManifest.remove_item(self._manifest, issue)

    @property
    def issues(self) -> List[str]:
        return self.manifest["items"]

    @property
    def provisional(self):
        return BundleManifest.get_component(self.manifest, "provisional")

    @provisional.setter
    def provisional(self, provisional: str) -> None:
        self.manifest = BundleManifest.set_component(
            self._manifest, "provisional", str(provisional)
        )

    @property
    def ahead_of_print_bundle(self) -> str:
        return BundleManifest.get_component(self.manifest, "aop", "")

    @ahead_of_print_bundle.setter
    def ahead_of_print_bundle(self, value: str) -> None:
        self.manifest = BundleManifest.set_component(self._manifest, "aop", str(value))

    def remove_ahead_of_print_bundle(self) -> None:
        self.manifest = BundleManifest.remove_component(self._manifest, "aop")
