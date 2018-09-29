import logging

from pyramid.config import Configurator
from pyramid.httpexceptions import (
    HTTPNotFound,
    HTTPNoContent,
    HTTPCreated,
    HTTPBadRequest,
)
from cornice import Service
from cornice.validators import colander_body_validator
from cornice.service import get_services
import colander
from slugify import slugify
from cornice_swagger import CorniceSwagger

from . import services
from . import adapters
from . import exceptions

LOGGER = logging.getLogger(__name__)

swagger = Service(name="OpenAPI", path="/__api__", description="OpenAPI documentation.")

documents = Service(
    name="documents",
    path="/documents/{document_id}",
    description="Get document at its latest version.",
)

manifest = Service(
    name="manifest",
    path="/documents/{document_id}/manifest",
    description="Get the document's manifest.",
)

assets_list = Service(
    name="assets_list",
    path="/documents/{document_id}/assets",
    description="Get the document's assets.",
)

assets = Service(
    name="assets",
    path="/documents/{document_id}/assets/{asset_slug}",
    description="Set the URL for an document's asset.",
)

diff = Service(
    name="diff",
    path="/documents/{document_id}/diff",
    description="Compare two versions of the same document.",
)


class Asset(colander.MappingSchema):
    asset_id = colander.SchemaNode(colander.String())
    asset_url = colander.SchemaNode(colander.String(), validator=colander.url)


class Assets(colander.SequenceSchema):
    asset = Asset()


class RegisterDocumentSchema(colander.MappingSchema):
    """Representa o schema de dados para registro de documentos.
    """

    data = colander.SchemaNode(colander.String(), validator=colander.url)
    assets = Assets()


class AssetSchema(colander.MappingSchema):
    """Representa o schema de dados para registro de ativos do documento.
    """

    asset_url = colander.SchemaNode(colander.String(), validator=colander.url)


@documents.get(accept="text/xml", renderer="xml")
def fetch_document_data(request):
    """Obtém o conteúdo do documento representado em XML com todos os
    apontamentos para seus ativos digitais contextualizados de acordo com a
    versão do documento. Produzirá uma resposta com o código HTTP 404 caso o
    documento solicitado não seja conhecido pela aplicação.
    """
    when = request.GET.get("when", None)
    if when:
        version = {"version_at": when}
    else:
        version = {}
    try:
        return request.services["fetch_document_data"](
            id=request.matchdict["document_id"], **version
        )
    except (exceptions.DocumentDoesNotExist, ValueError) as exc:
        raise HTTPNotFound(exc)


@documents.put(schema=RegisterDocumentSchema(), validators=(colander_body_validator,))
def put_document(request):
    """Adiciona ou atualiza registro de documento. A atualização do documento é 
    idempotente.
    
    A semântica desta view-function está definida conforme a especificação:
    https://www.w3.org/Protocols/rfc2616/rfc2616-sec9.html#sec9.6

    Em resumo, ``PUT /documents/:doc_id`` com o payload válido de acordo com o 
    schema documentado, e para um ``:doc_id`` inédito, resultará no registro do 
    documento e produzirá uma resposta com o código HTTP 201 Created. Qualquer
    requisição subsequente para o mesmo recurso produzirá respostas com o código
    HTTP 204 No Content.
    """
    data_url = request.validated["data"]
    assets = {
        asset["asset_id"]: asset["asset_url"]
        for asset in request.validated.get("assets", [])
    }
    try:
        request.services["register_document"](
            id=request.matchdict["document_id"], data_url=data_url, assets=assets
        )
    except exceptions.DocumentAlreadyExists:
        try:
            request.services["register_document_version"](
                id=request.matchdict["document_id"], data_url=data_url, assets=assets
            )
        except exceptions.VersionAlreadySet as exc:
            LOGGER.info(
                'skipping request to add version to "%s": %s',
                request.matchdict["document_id"],
                exc,
            )
        return HTTPNoContent("document updated successfully")
    else:
        return HTTPCreated("document created successfully")


@manifest.get(accept="application/json", renderer="json")
def get_manifest(request):
    """Obtém o manifesto do documento. Produzirá uma resposta com o código
    HTTP 404 caso o documento não seja conhecido pela aplicação.
    """
    try:
        return request.services["fetch_document_manifest"](
            id=request.matchdict["document_id"]
        )
    except exceptions.DocumentDoesNotExist as exc:
        raise HTTPNotFound(exc)


def slugify_assets_ids(assets, slug_fn=slugify):
    return [
        {"slug": slug_fn(asset_id), "id": asset_id, "url": asset_url}
        for asset_id, asset_url in assets.items()
    ]


@assets_list.get(accept="application/json", renderer="json")
def get_assets_list(request):
    """Obtém relação dos ativos associados ao documento em determinada
    versão. Produzirá uma resposta com o código HTTP 404 caso o documento não
    seja conhecido pela aplicação.
    """
    try:
        assets = request.services["fetch_assets_list"](
            id=request.matchdict["document_id"]
        )
    except exceptions.DocumentDoesNotExist as exc:
        raise HTTPNotFound(exc)

    assets["assets"] = slugify_assets_ids(assets["assets"])
    return assets


@assets.put(schema=AssetSchema(), validators=(colander_body_validator,))
def put_asset(request):
    """Adiciona ou atualiza registro de ativo do documento. A atualização do 
    ativo é idempotente.
    
    A semântica desta view-function está definida conforme a especificação:
    https://www.w3.org/Protocols/rfc2616/rfc2616-sec9.html#sec9.6
    """
    assets_list = get_assets_list(request)
    assets_map = {asset["slug"]: asset["id"] for asset in assets_list["assets"]}
    asset_slug = request.matchdict["asset_slug"]
    try:
        asset_id = assets_map[asset_slug]
    except KeyError:
        raise HTTPNotFound(
            'cannot fetch asset with slug "%s": asset does not exist' % asset_slug
        )

    asset_url = request.validated["asset_url"]
    try:
        request.services["register_asset_version"](
            id=request.matchdict["document_id"], asset_id=asset_id, asset_url=asset_url
        )
    except exceptions.VersionAlreadySet as exc:
        LOGGER.info(
            'skipping request to add version to "%s/assets/%s": %s',
            request.matchdict["document_id"],
            request.matchdict["asset_slug"],
            exc,
        )
    return HTTPNoContent("asset updated successfully")


@diff.get(renderer="text")
def diff_document_versions(request):
    """Compara duas versões do documento. Se o argumento `to_when` não for
    fornecido, será assumido como alvo a versão mais recente.
    """
    from_when = request.GET.get("from_when", None)
    if from_when is None:
        raise HTTPBadRequest("cannot fetch diff: missing attribute from_when")
    try:
        return request.services["diff_document_versions"](
            id=request.matchdict["document_id"],
            from_version_at=from_when,
            to_version_at=request.GET.get("to_when", None),
        )
    except (exceptions.DocumentDoesNotExist, ValueError) as exc:
        raise HTTPNotFound(exc)


@swagger.get()
def openAPI_spec(request):
    doc = CorniceSwagger(get_services())
    doc.summary_docstrings = True
    return doc.generate("document_store", "0.1")


class XMLRenderer:
    """Renderizador para dados do tipo ``text/xml``.

    Espera que o retorno da view-function seja uma string de bytes pronta para
    ser transferida para o cliente. Este renderer apenas define o content-type
    da resposta HTTP.
    """

    def __init__(self, info):
        pass

    def __call__(self, value, system):
        request = system.get("request")
        if request is not None:
            request.response.content_type = "text/xml"
        return value


class PlainTextRenderer:
    """Renderizador para dados do tipo ``text/plain``.

    Espera que o retorno da view-function seja uma string de bytes pronta para
    ser transferida para o cliente. Este renderer apenas define o content-type
    da resposta HTTP.
    """

    def __init__(self, info):
        pass

    def __call__(self, value, system):
        request = system.get("request")
        if request is not None:
            request.response.content_type = "text/plain"
        return value


def main(global_config, **settings):
    config = Configurator(settings=settings)
    config.include("cornice")
    config.include("cornice_swagger")
    config.scan()
    config.add_renderer("xml", XMLRenderer)
    config.add_renderer("text", PlainTextRenderer)

    mongo = adapters.MongoDB("mongodb://db:27017/")
    Session = adapters.Session.partial(mongo)

    config.add_request_method(
        lambda request: services.get_handlers(Session), "services", reify=True
    )

    return config.make_wsgi_app()
