import json
import logging
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import List

from django.conf import settings
from django.contrib.gis.geos import Polygon
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Query
from ninja.errors import ValidationError
from ninja.pagination import paginate
from ninja.security import APIKeyHeader

from ohmg.accounts.models import (
    APIKey,
    User,
)
from ohmg.core.models import (
    Document,
    Layer,
    LayerSet,
    Map,
    Region,
)
from ohmg.georeference.models import (
    Job,
    SessionBase,
    SessionLock,
)
from ohmg.places.models import Place

from .filters import (
    FilterDocumentSchema,
    FilterJobSchema,
    FilterLayerSchema,
    FilterRegionSchema,
    FilterSessionSchema,
)
from .paginators import (
    DocumentPagination,
    LayerPagination,
    MapPagination,
    ProfilePagination,
    SessionPagination,
)
from .schemas import (
    DocumentSchema,
    JobSchema,
    LayerSchema,
    LayerSetSchema,
    MapFullSchema,
    MapListSchema2,
    PlaceFullSchema,
    PlaceSchema,
    RegionSchema,
    SessionLockSchema,
    SessionSchema,
    UserSchema,
)
from .search import search_fuzzy_first_word, search_word_contains

logger = logging.getLogger(__name__)


class APIKeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        if key == settings.OHMG_API_KEY:
            return key
        elif key in APIKey.objects.all().values_list("value", flat=True):
            APIKey.objects.get(value=key).increment_count()
            if key in APIKey.objects.filter(active=True).values_list("value", flat=True):
                return key


# going to be useful eventually for Geo support
# https://github.com/vitalik/django-ninja/issues/335
beta2 = NinjaAPI(
    auth=APIKeyAuth(),
    title="OldInsuranceMaps.net API",
    version="beta2",
    description="An API for accessing content on OldInsuranceMaps.net.",
)


@beta2.exception_handler(ValidationError)
def api_errors(request, exc):
    logger.warning(exc.errors)
    return beta2.create_response(request, {"detail": exc.errors}, status=422)


@beta2.get("jobs/", response=List[JobSchema], url_name="job_list")
@paginate
def list_jobs(request, filters: FilterJobSchema = Query(...)):
    queryset = Job.objects.all().order_by("-date_queued")
    queryset = filters.filter(queryset)
    return queryset


@beta2.get("sessions/", response=List[SessionSchema], url_name="session_list")
@paginate(SessionPagination)
def list_sessions(request, filters: FilterSessionSchema = Query(...), date_range: str = ""):
    sort_param = request.GET.get("sortby", "")
    sort_dir = request.GET.get("sort", "")
    if date_range:
        start, end = date_range.split(",")
        if start == end:
            sessions = SessionBase.objects.filter(date_created__contains=start)
        else:
            sessions = SessionBase.objects.filter(date_created__range=[start, end])
    else:
        sessions = SessionBase.objects.all()
    if sort_param:
        if sort_param == "user":
            sort_param = "user__username"
        if sort_param == "duration":
            sort_param = "user_input_duration"
        sort_arg = sort_param if sort_dir == "asc" else f"-{sort_param}"
        queryset = sessions.order_by(sort_arg).select_related("doc2", "reg2", "lyr2")
    else:
        queryset = sessions.order_by("-date_created").select_related("doc2", "reg2", "lyr2")
    queryset = filters.filter(queryset)
    return queryset


@beta2.get("session/", response=SessionSchema, url_name="session_detail")
def session_details(request, id: int):
    return get_object_or_404(SessionBase.objects.prefetch_related(), pk=id)


@beta2.get("users/", response=List[UserSchema], url_name="user_list")
def list_users(request):
    queryset = User.objects.all().exclude(username="AnonymousUser").order_by("username")
    return queryset


@beta2.get("profiles/", response=List[UserSchema], url_name="user_list")
@paginate(ProfilePagination)
def list_profiles(
    request,
    sort: str = "",
    sortby: str = "",
):
    users = User.objects.filter(
        Q(load_ct__gt=0) | Q(psesh_ct__gt=0) | Q(gsesh_ct__gt=0) | Q(gcp_ct__gt=0)
    )
    if sortby:
        sort_arg = sortby if sort == "asc" else f"-{sortby}"
        queryset = users.order_by(sort_arg)
    else:
        queryset = users.order_by("username")

    if search_param := request.GET.get("search"):
        queryset = search_word_contains(search_param, "username", queryset)

    return queryset


@beta2.get("map/", response=MapFullSchema, url_name="map")
def get_map(request, map: str):
    return get_object_or_404(Map, pk=map)


@beta2.get("maps2/", response=List[MapListSchema2], url_name="maps_list2")
@paginate(MapPagination)
def list_maps2(
    request,
    place: str = None,
    place_inclusive: bool = False,
    loaded: bool = False,
    loaded_by: str = "",
    sort: str = "",
    sortby: str = "",
    source: str = "",
):
    if loaded:
        maps = Map.objects.filter(load_date__isnull=False)
    else:
        maps = Map.objects.all()
    if loaded_by:
        maps = maps.filter(loaded_by__username=loaded_by)
    if place:
        if place_inclusive:
            p = Place.objects.get(slug=place)
            place_ids = p.get_inclusive_pks()
            maps = maps.filter(locales__in=place_ids)
        else:
            maps = maps.filter(locales__slug__exact=place)
    if source:
        if source == "none":
            maps = maps.filter(source__isnull=True)
        else:
            maps = maps.filter(source_id=source)
    if sortby:
        if sortby == "loaded_by":
            sortby = "loaded_by__username"
        sort_arg = sortby if sort == "asc" else f"-{sortby}"
        queryset = maps.order_by(sort_arg)
    else:
        queryset = maps.order_by("title")

    if search_param := request.GET.get("search"):
        queryset = search_fuzzy_first_word(search_param, ["title"], queryset)
    return queryset


@beta2.get("layerset/", response=LayerSetSchema, url_name="layerset")
def get_layerset(
    request,
    map: str,
    category: str,
):
    return LayerSet.objects.get(category__slug=category, map_id=map)


@beta2.get("layersets/", response=List[LayerSetSchema], url_name="layersets")
def get_layersets(
    request,
    map: str,
):
    return LayerSet.objects.filter(map_id=map)


@beta2.get("place/", response=PlaceFullSchema, url_name="place")
def place(request, slug: str):
    return get_object_or_404(Place.objects.prefetch_related(), slug=slug)


@beta2.get("places/", response=List[PlaceSchema], url_name="place_list")
def list_places(request):
    queryset = Place.objects.all().exclude(volume_count=0).order_by("name")
    return queryset


@lru_cache(maxsize=4)
def _place_coordinates(path: str, mtime: float) -> dict:
    """{place slug: [lon, lat]} from PLACE_COORDINATES_FILE (cached until the file changes)."""
    data = json.loads(Path(path).read_text())
    return {
        f["properties"]["slug"]: f["geometry"]["coordinates"]
        for f in data.get("features", [])
        if f.get("geometry") and f.get("properties", {}).get("slug")
    }


def _places_geojson_with_progress() -> dict:
    """Every place that has (non-hidden) maps, with per-volume georeferencing progress.

    Point location: the place's entry in PLACE_COORDINATES_FILE, else the centroid of a
    georeferenced extent (main content preferred over key map). "status" is "main" if any volume
    has georeferenced main content, "key" if only key maps are georeferenced, else "none".
    """
    path = Path(settings.PLACE_COORDINATES_FILE)
    coords = _place_coordinates(str(path), path.stat().st_mtime) if path.exists() else {}

    layersets = defaultdict(dict)
    for ls in (
        LayerSet.objects.filter(category__slug__in=["main-content", "key-map"])
        .annotate(n_layers=Count("layer"), n_pages=Count("layer__region__document", distinct=True))
        .values("map_id", "category__slug", "extent", "n_layers", "n_pages")
    ):
        layersets[ls["map_id"]][ls["category__slug"]] = ls

    places = {}
    maps = (
        Map.objects.filter(hidden=False)
        .annotate(n_docs=Count("documents", distinct=True))
        .prefetch_related("locales")
        .order_by("year", "volume_number")
    )
    for map in maps:
        main = layersets[map.pk].get("main-content") or {}
        key = layersets[map.pk].get("key-map") or {}
        n_main, n_key = main.get("n_layers") or 0, key.get("n_layers") or 0
        for place in map.locales.all():
            entry = places.setdefault(
                place.slug,
                {
                    "display_name": place.display_name,
                    "maps": 0,
                    "years": [],
                    "volumes": [],
                    "extent": None,
                },
            )
            entry["maps"] += 1
            entry["years"].append(
                f"{map.year} vol. {map.volume_number}" if map.volume_number else map.year
            )
            if not (n_main or n_key):
                continue
            entry["volumes"].append(
                {
                    "year": f"{map.year} vol. {map.volume_number}"
                    if map.volume_number
                    else map.year,
                    "url": f"/map/{map.pk}",
                    "title": map.title,
                    "sheets": main.get("n_pages") or 0,
                    "pages": map.n_docs,
                    "key_map": n_key > 0,
                }
            )
            extent = (n_main and main.get("extent")) or (n_key and key.get("extent"))
            if extent and not entry["extent"]:
                entry["extent"] = extent

    features = []
    for slug, entry in places.items():
        point = coords.get(slug)
        if not point and entry["extent"]:
            point = Polygon().from_bbox(entry["extent"]).centroid.coords
        if not point:
            continue
        has_main = any(v["sheets"] for v in entry["volumes"])
        has_key = any(v["key_map"] for v in entry["volumes"])
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": point},
                "properties": {
                    "status": "main" if has_main else ("key" if has_key else "none"),
                    "volumes": entry["volumes"],
                    "place": {
                        "display_name": entry["display_name"],
                        "url": f"/{slug}/",
                        "viewer_url": f"/viewer/{slug}" if has_main else None,
                        "map_count": entry["maps"],
                        "map_years": entry["years"],
                    },
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


@beta2.get("places/geojson/", url_name="places_geojson")
def get_places_geojson(request):
    """Generate geojson for all places with their maps."""

    if settings.PLACE_COORDINATES_FILE:
        return _places_geojson_with_progress()

    place_dict = {}
    for ls in (
        LayerSet.objects.filter(category__slug="main-content")
        .prefetch_related()
        .annotate(
            locale=F("map__locales"),
            locale_name=F("map__locales__display_name"),
            locale_slug=F("map__locales__slug"),
            map_year=F("map__year"),
            map_volume_number=F("map__volume_number"),
        )
    ):
        if ls.locale and ls.extent:
            place_entry = place_dict.get(
                ls.locale_slug,
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": Polygon().from_bbox(ls.extent).centroid.coords,
                    },
                    "properties": {
                        "volumes": [],
                        "place": {
                            "url": f"/viewer/{ls.locale_slug}",
                            "display_name": ls.locale_name,
                        },
                    },
                },
            )
            year_vol = (
                f"{ls.map_year} vol. {ls.map_volume_number}"
                if ls.map_volume_number
                else ls.map_year
            )
            place_entry["properties"]["volumes"].append(
                {
                    "year": year_vol,
                    "url": f"/map/{ls.map_id}",
                }
            )
            place_dict[ls.locale_slug] = place_entry
    geojson = {
        "type": "FeatureCollection",
        "features": list(place_dict.values()),
    }
    return geojson


## DOCUMENT ROUTES
@beta2.get("document/", response=DocumentSchema, url_name="document")
def document(request, id: int):
    return get_object_or_404(Document.objects.prefetch_related(), pk=id)


@beta2.get("documents/", response=List[DocumentSchema], url_name="api_documents_list")
@paginate(DocumentPagination)
def documents(request, filters: FilterDocumentSchema = Query(...)):
    sort_param = request.GET.get("sortby", "")
    sort_dir = request.GET.get("sort", "")
    queryset = Document.objects.all().prefetch_related()

    if sort_param:
        if sort_param == "map":
            sort_param = "map__title"
        sort_arg = sort_param if sort_dir == "asc" else f"-{sort_param}"
        queryset = queryset.order_by(sort_arg)
    else:
        queryset = queryset.order_by("map__title", "page_number")

    queryset = filters.filter(queryset)

    if search_param := request.GET.get("search"):
        search_field = request.GET.get("field", "title").split(",")
        queryset = search_fuzzy_first_word(search_param, search_field, queryset)

    return queryset


## REGION ROUTES - WIP
@beta2.get("region/", response=RegionSchema, url_name="regions")
def region(request, id: int):
    return get_object_or_404(Region.objects.prefetch_related(), pk=id)


@beta2.get("regions/", response=List[DocumentSchema], url_name="documents")
def regions(request, filters: FilterRegionSchema = Query(...)):
    queryset = Document.objects.all().prefetch_related()
    queryset = filters.filter(queryset)
    return queryset


@beta2.get("layers/", response=List[LayerSchema], url_name="layers")
def layer(request, map: str):
    return Layer.objects.filter(region__document__map_id=map)


@beta2.get("layers2/", response=List[LayerSchema], url_name="api_layers_list")
@paginate(LayerPagination)
def list_layers(request, filters: FilterLayerSchema = Query(...)):
    sort_param = request.GET.get("sortby", "")
    sort_dir = request.GET.get("sort", "")
    queryset = Layer.objects.all()
    if sort_param:
        if sort_param == "map":
            sort_param = "region__document__map__title"
        if sort_dir == "asc":
            queryset = queryset.order_by(F(sort_param).asc(nulls_last=True)).select_related(
                "region"
            )
        else:
            queryset = queryset.order_by(F(sort_param).desc(nulls_last=True)).select_related(
                "region"
            )

    else:
        queryset = queryset.order_by("-pk").select_related("region")

    queryset = filters.filter(queryset)

    if search_param := request.GET.get("search"):
        search_field = request.GET.get("field", "title").split(",")
        queryset = search_fuzzy_first_word(search_param, search_field, queryset)

    return queryset


## SESSION LOCKS
@beta2.get("session-locks/", response=List[SessionLockSchema], url_name="session_locks")
def session_locks(request, map: str = None):
    locks = SessionLock.objects.all().prefetch_related()
    if map:
        locks = [i for i in locks if i.target.map.pk == map]
    return locks
