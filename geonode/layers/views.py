# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import os
import sys
import logging
import shutil
import traceback
import csv
import datetime
from pprint import pprint
from guardian.shortcuts import get_perms
from unidecode import unidecode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.shortcuts import (
    redirect, get_object_or_404, render, render_to_response)
from django.conf import settings
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.utils.html import escape
from django.template.defaultfilters import slugify
from django.forms.models import inlineformset_factory
from django.db.models import F
from django.forms.util import ErrorList

from geonode.tasks.deletion import delete_layer
from geonode.services.models import Service
from geonode.layers.forms import LayerForm, LayerUploadForm, NewLayerUploadForm, LayerAttributeForm
from geonode.base.forms import CategoryForm
from geonode.layers.models import Layer, Attribute, UploadSession
from geonode.base.enumerations import CHARSETS
from geonode.base.models import TopicCategory
from geonode.cephgeo.models import FTPRequest
from geonode.cephgeo.utils import get_ftp_details
from geonode.datarequests.utils import get_area_coverage

from geonode.utils import default_map_config
from geonode.utils import GXPLayer
from geonode.utils import GXPMap
from geonode.layers.utils import file_upload, is_raster, is_vector
from geonode.utils import resolve_object, llbbox_to_mercator
from geonode.people.forms import ProfileForm, PocForm
from geonode.security.views import _perms_info_json
from geonode.documents.models import get_related_documents
from geonode.utils import build_social_links
from geonode.geoserver.helpers import cascading_delete, gs_catalog
from urlparse import urljoin, urlsplit
from actstream.signals import action
from actstream.models import Action

from .forms import AnonDownloaderForm
from geonode.eula.models import AnonDownloader

# from dateftime import date, timedelta, datetime
from django.utils import timezone
from geonode.people.models import Profile

from geonode.reports.models import DownloadTracker
from geonode.base.models import ResourceBase

CONTEXT_LOG_FILE = None

if 'geonode.geoserver' in settings.INSTALLED_APPS:
    from geonode.geoserver.helpers import _render_thumbnail
    from geonode.geoserver.helpers import ogc_server_settings
    CONTEXT_LOG_FILE = ogc_server_settings.LOG_FILE

logger = logging.getLogger("geonode.layers.views")

DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")

_PERMISSION_MSG_DELETE = _("You are not permitted to delete this layer")
_PERMISSION_MSG_GENERIC = _('You do not have permissions for this layer.')
_PERMISSION_MSG_MODIFY = _("You are not permitted to modify this layer")
_PERMISSION_MSG_METADATA = _(
    "You are not permitted to modify this layer's metadata")
_PERMISSION_MSG_VIEW = _("You are not permitted to view this layer")


def log_snippet(log_file):
    if not os.path.isfile(log_file):
        return "No log file at %s" % log_file

    with open(log_file, "r") as f:
        f.seek(0, 2)  # Seek @ EOF
        fsize = f.tell()  # Get Size
        f.seek(max(fsize - 10024, 0), 0)  # Set pos @ last n chars
        return f.read()


def _resolve_layer(request, typename, permission='base.view_resourcebase',
                   msg=_PERMISSION_MSG_GENERIC, **kwargs):
    """
    Resolve the layer by the provided typename (which may include service name) and check the optional permission.
    """
    service_typename = typename.split(":", 1)

    if Service.objects.filter(name=service_typename[0]).exists():
        service = Service.objects.filter(name=service_typename[0])
        return resolve_object(request,
                              Layer,
                              {'service': service[0],
                               'typename': service_typename[1] if service[0].method != "C" else typename},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)
    else:
        return resolve_object(request,
                              Layer,
                              {'typename': typename,
                               'service': None},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)


@login_required
def layer_upload(request, template='upload/layer_upload.html'):
    print('layer_upload exec')
    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS,
            'is_layer': True,
        }
        return render_to_response(template, RequestContext(request, ctx))
    elif request.method == 'POST':
        form = NewLayerUploadForm(request.POST, request.FILES)
        tempdir = None
        errormsgs = []
        out = {'success': False}
        if form.is_valid():
            title = form.cleaned_data["layer_title"]
            # Replace dots in filename - GeoServer REST API upload bug
            # and avoid any other invalid characters.
            # Use the title if possible, otherwise default to the filename
            if title is not None and len(title) > 0:
                name_base = title
            else:
                name_base, __ = os.path.splitext(
                    form.cleaned_data["base_file"].name)
            name = slugify(name_base.replace(".", "_"))
            try:
                # Moved this inside the try/except block because it can raise
                # exceptions when unicode characters are present.
                # This should be followed up in upstream Django.
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(
                    base_file,
                    name=name,
                    user=request.user,
                    overwrite=False,
                    charset=form.cleaned_data["charset"],
                    abstract=form.cleaned_data["abstract"],
                    title=form.cleaned_data["layer_title"],
                )
            except Exception as e:
                exception_type, error, tb = sys.exc_info()
                logger.exception(e)
                out['success'] = False
                out['errors'] = str(error)
                # Assign the error message to the latest UploadSession from
                # that user.
                latest_uploads = UploadSession.objects.filter(
                    user=request.user).order_by('-date')
                if latest_uploads.count() > 0:
                    upload_session = latest_uploads[0]
                    upload_session.error = str(error)
                    upload_session.traceback = traceback.format_exc(tb)
                    upload_session.context = log_snippet(CONTEXT_LOG_FILE)
                    upload_session.save()
                    out['traceback'] = upload_session.traceback
                    out['context'] = upload_session.context
                    out['upload_session'] = upload_session.id
            else:
                out['success'] = True
                if hasattr(saved_layer, 'info'):
                    out['info'] = saved_layer.info
                out['url'] = reverse(
                    'layer_detail', args=[
                        saved_layer.service_typename])
                upload_session = saved_layer.upload_session
                upload_session.processed = True
                upload_session.save()
                permissions = form.cleaned_data["permissions"]
                if permissions is not None and len(permissions.keys()) > 0:
                    saved_layer.set_permissions(permissions)
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])
            out['errors'] = form.errors
            out['errormsgs'] = errormsgs
        if out['success']:
            status_code = 200
        else:
            status_code = 400
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


def layer_detail(request, layername, template='layers/layer_detail.html'):
    # tile shapefile ng settings.tile
    layer = _resolve_layer(
        request,
        layername,
        'base.view_resourcebase',
        _PERMISSION_MSG_VIEW)

    # check if problematic in geoserver
    cat = gs_catalog
    try:
        gs_layer = cat.get_layer(layername)
        gs_layer.resource.latlon_bbox
    except:
        print 'GEOSERVER LAYER ERROR'
        return HttpResponse(loader.render_to_string('layers/layer_error.html', RequestContext(request, {'error_message': _("Error in layer.")})), status=404)
        # return HttpResponse(status=404)

    # assert False, str(layer_bbox)
    config = layer.attribute_config()
    # print layername
    # Add required parameters for GXP lazy-loading
    layer_bbox = layer.bbox
    bbox = [float(coord) for coord in list(layer_bbox[0:4])]
    config["srs"] = getattr(settings, 'DEFAULT_MAP_CRS', 'EPSG:900913')
    config["bbox"] = bbox if config["srs"] != 'EPSG:900913' \
        else llbbox_to_mercator([float(coord) for coord in bbox])
    config["title"] = layer.title
    config["queryable"] = True

    if layer.storeType == "remoteStore":
        service = layer.service
        source_params = {
            "ptype": service.ptype,
            "remote": True,
            "url": service.base_url,
            "name": service.name}
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config),
            source_params=json.dumps(source_params))
    else:
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config))

    # Update count for popularity ranking,
    # but do not includes admins or resource owners
    if request.user != layer.owner and not request.user.is_superuser:
        Layer.objects.filter(
            id=layer.id).update(popular_count=F('popular_count') + 1)

    # center/zoom don't matter; the viewer will center on the layer bounds
    map_obj = GXPMap(projection=getattr(
        settings, 'DEFAULT_MAP_CRS', 'EPSG:900913'))

    NON_WMS_BASE_LAYERS = [
        la for la in default_map_config()[1] if la.ows_url is None]

    metadata = layer.link_set.metadata().filter(
        name__in=settings.DOWNLOAD_FORMATS_METADATA)

    context_dict = {
        "resource": layer,
        'perms_list': get_perms(request.user, layer.get_self_resource()),
        "permissions_json": _perms_info_json(layer),
        "documents": get_related_documents(layer),
        "metadata": metadata,
        "is_layer": True,
        "wps_enabled": settings.OGC_SERVER['default']['WPS_ENABLED'],
    }
    context_dict["phillidar2keyword"] = "PhilLiDAR2"
    context_dict["phillidar1keyword"] = "UPD"

    context_dict["viewer"] = json.dumps(
        map_obj.viewer_json(request.user, * (NON_WMS_BASE_LAYERS + [maplayer])))
    context_dict["preview"] = getattr(
        settings,
        'LAYER_PREVIEW_LIBRARY',
        'leaflet')

    #pprint('CONTEXT DICTIONARY')
    #pprint(context_dict)
    #pprint('END')
    if request.user.has_perm('download_resourcebase', layer.get_self_resource()):
        if layer.storeType == 'dataStore':
            links = layer.link_set.download().filter(
                name__in=settings.DOWNLOAD_FORMATS_VECTOR)
        else:
            links = layer.link_set.download().filter(
                name__in=settings.DOWNLOAD_FORMATS_RASTER)
        context_dict["links"] = links

    if settings.SOCIAL_ORIGINS:
        context_dict["social_links"] = build_social_links(request, layer)

    if request.method == 'POST':
        #pprint(request.POST)
        form = AnonDownloaderForm(request.POST)
        out = {}
        if form.is_valid():
            #pprint(form)
            out['success'] = True
            anondownload = form.save()
            anondownload.anon_layer = Layer.objects.get(
                typename=layername).typename
            anondownload.save()
        else:
            #pprint(form)
            errormsgs = []
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])
            out['success'] = False
            out['errors'] = form.errors
            out['errormsgs'] = errormsgs
        if out['success']:
            status_code = 200
        else:
            status_code = 400
        # Handle form
        pprint(status_code)
        return HttpResponse(status=status_code)
    else:
        # Render form
        form = AnonDownloaderForm()
    context_dict["anon_form"] = form
    context_dict["layername"] = layername
    return render_to_response(template, RequestContext(request, context_dict))


@login_required
def layer_metadata(request, layername, template='layers/layer_metadata.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase_metadata',
        _PERMISSION_MSG_METADATA)
    layer_attribute_set = inlineformset_factory(
        Layer,
        Attribute,
        extra=0,
        form=LayerAttributeForm,
    )
    topic_category = layer.category

    poc = layer.poc
    metadata_author = layer.metadata_author

    if request.method == "POST":
        layer_form = LayerForm(request.POST, instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(
            request.POST,
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            request.POST,
            prefix="category_choice_field",
            initial=int(
                request.POST["category_choice_field"]) if "category_choice_field" in request.POST else None)

    else:
        layer_form = LayerForm(instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            prefix="category_choice_field",
            initial=topic_category.id if topic_category else None)

    if request.method == "POST" and layer_form.is_valid(
    ) and attribute_form.is_valid() and category_form.is_valid():
        new_poc = layer_form.cleaned_data['poc']
        new_author = layer_form.cleaned_data['metadata_author']
        new_keywords = layer_form.cleaned_data['keywords']

        if new_poc is None:
            if poc is None:
                poc_form = ProfileForm(
                    request.POST,
                    prefix="poc",
                    instance=poc)
            else:
                poc_form = ProfileForm(request.POST, prefix="poc")
            if poc_form.is_valid():
                if len(poc_form.cleaned_data['profile']) == 0:
                    # FIXME use form.add_error in django > 1.7
                    errors = poc_form._errors.setdefault(
                        'profile', ErrorList())
                    errors.append(
                        _('You must set a point of contact for this resource'))
                    poc = None
            if poc_form.has_changed and poc_form.is_valid():
                new_poc = poc_form.save()

        if new_author is None:
            if metadata_author is None:
                author_form = ProfileForm(request.POST, prefix="author",
                                          instance=metadata_author)
            else:
                author_form = ProfileForm(request.POST, prefix="author")
            if author_form.is_valid():
                if len(author_form.cleaned_data['profile']) == 0:
                    # FIXME use form.add_error in django > 1.7
                    errors = author_form._errors.setdefault(
                        'profile', ErrorList())
                    errors.append(
                        _('You must set an author for this resource'))
                    metadata_author = None
            if author_form.has_changed and author_form.is_valid():
                new_author = author_form.save()

        new_category = TopicCategory.objects.get(
            id=category_form.cleaned_data['category_choice_field'])

        for form in attribute_form.cleaned_data:
            la = Attribute.objects.get(id=int(form['id'].id))
            la.description = form["description"]
            la.attribute_label = form["attribute_label"]
            la.visible = form["visible"]
            la.display_order = form["display_order"]
            la.save()

        if new_poc is not None and new_author is not None:
            new_keywords = layer_form.cleaned_data['keywords']
            layer.keywords.clear()
            layer.keywords.add(*new_keywords)
            the_layer = layer_form.save()
            up_sessions = UploadSession.objects.filter(layer=the_layer.id)
            if up_sessions.count() > 0 and up_sessions[0].user != the_layer.owner:
                up_sessions.update(user=the_layer.owner)
            the_layer.poc = new_poc
            the_layer.metadata_author = new_author
            Layer.objects.filter(id=the_layer.id).update(
                category=new_category
            )

            if getattr(settings, 'SLACK_ENABLED', False):
                try:
                    from geonode.contrib.slack.utils import build_slack_message_layer, send_slack_messages
                    send_slack_messages(
                        build_slack_message_layer("layer_edit", the_layer))
                except:
                    print "Could not send slack message."

            return HttpResponseRedirect(
                reverse(
                    'layer_detail',
                    args=(
                        layer.service_typename,
                    )))

    if poc is not None:
        layer_form.fields['poc'].initial = poc.id
        poc_form = ProfileForm(prefix="poc")
        poc_form.hidden = True

    if metadata_author is not None:
        layer_form.fields['metadata_author'].initial = metadata_author.id
        author_form = ProfileForm(prefix="author")
        author_form.hidden = True

    return render_to_response(template, RequestContext(request, {
        "layer": layer,
        "layer_form": layer_form,
        "poc_form": poc_form,
        "author_form": author_form,
        "attribute_form": attribute_form,
        "category_form": category_form,
    }))


@login_required
def layer_change_poc(request, ids, template='layers/layer_change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            # Redirect after POST
            return HttpResponseRedirect('/admin/maps/layer')
    else:
        form = PocForm()  # An unbound form
    return render_to_response(
        template, RequestContext(
            request, {
                'layers': layers, 'form': form}))


@login_required
def layer_replace(request, layername, template='layers/layer_replace.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase',
        _PERMISSION_MSG_MODIFY)

    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS,
            'layer': layer,
            'is_featuretype': layer.is_vector(),
            'is_layer': True,
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':

        form = LayerUploadForm(request.POST, request.FILES)
        tempdir = None
        out = {}

        if form.is_valid():
            try:
                tempdir, base_file = form.write_files()
                if layer.is_vector() and is_raster(base_file):
                    out['success'] = False
                    out['errors'] = _(
                        "You are attempting to replace a vector layer with a raster.")
                elif (not layer.is_vector()) and is_vector(base_file):
                    out['success'] = False
                    out['errors'] = _(
                        "You are attempting to replace a raster layer with a vector.")
                else:
                    # delete geoserver's store before upload
                    cat = gs_catalog
                    cascading_delete(cat, layer.typename)
                    saved_layer = file_upload(
                        base_file,
                        name=layer.name,
                        user=request.user,
                        overwrite=True,
                        charset=form.cleaned_data["charset"],
                    )
                    out['success'] = True
                    out['url'] = reverse(
                        'layer_detail', args=[
                            saved_layer.service_typename])
            except Exception as e:
                out['success'] = False
                out['errors'] = str(e)
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            errormsgs = []
            for e in form.errors.values():
                errormsgs.append([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 400
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


@login_required
def layer_remove(request, layername, template='layers/layer_remove.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.delete_resourcebase',
        _PERMISSION_MSG_DELETE)

    if (request.method == 'GET'):
        return render_to_response(template, RequestContext(request, {
            "layer": layer
        }))
    if (request.method == 'POST'):
        try:
            delete_layer.delay(object_id=layer.id)
        except Exception as e:
            message = '{0}: {1}.'.format(
                _('Unable to delete layer'), layer.typename)

            if 'referenced by layer group' in getattr(e, 'message', ''):
                message = _('This layer is a member of a layer group, you must remove the layer from the group '
                            'before deleting.')

            messages.error(request, message)
            return render_to_response(template, RequestContext(request, {"layer": layer}))
        return HttpResponseRedirect(reverse("layer_browse"))
    else:
        return HttpResponse("Not allowed", status=403)


def layer_thumbnail(request, layername):
    if request.method == 'POST':
        layer_obj = _resolve_layer(request, layername)
        try:
            image = _render_thumbnail(request.body)

            if not image:
                return
            filename = "layer-%s-thumb.png" % layer_obj.uuid
            layer_obj.save_thumbnail(filename, image)

            return HttpResponse('Thumbnail saved')
        except:
            return HttpResponse(
                content='error saving thumbnail',
                status=500,
                mimetype='text/plain'
            )


def layer_download(request, layername):
    layer = _resolve_layer(
        request,
        layername,
        'base.view_resourcebase',
        _PERMISSION_MSG_VIEW)
    # pprint(request.user.is_authenticated)
    # if request.user.is_authenticated():
    #     action.send(request.user, verb='downloaded', action_object=layer)
    #     DownloadTracker(actor=Profile.objects.get(username=request.user),
    #                     title=str(layername),
    #                     resource_type=str(ResourceBase.objects.get(layer__typename=layername).csw_type),
    #                     keywords=Layer.objects.get(typename=layername).keywords.slugs()
    #                     ).save()
    #     pprint('Download Tracked') #Download tracking moved to layer_tracker

    splits = request.get_full_path().split("/")
    redir_url = urljoin(settings.OGC_SERVER['default'][
                        'PUBLIC_LOCATION'], "/".join(splits[4:]))
    return HttpResponseRedirect(redir_url)


def layer_tracker(request, layername, dl_type):
    layer = _resolve_layer(
        request,
        layername,
        'base.view_resourcebase',
        _PERMISSION_MSG_VIEW)
    pprint(request.user.is_authenticated)
    if request.user.is_authenticated():
        action.send(request.user, verb='downloaded', action_object=layer)
        DownloadTracker(actor=Profile.objects.get(username=request.user),
                        title=str(layername),
                        resource_type=str(ResourceBase.objects.get(
                            layer__typename=layername).csw_type),
                        keywords=Layer.objects.get(
                            typename=layername).keywords.slugs(),
                        dl_type=dl_type
                        ).save()
        #pprint('Download Tracked')
    return HttpResponse(status=200)


@login_required
def layer_download_csv(request):
    if not request.user.is_superuser:
        return HttpResponseRedirect("/forbidden/")
    response = HttpResponse(content_type='text/csv')
    datetoday = timezone.now()
    response['Content-Disposition'] = 'attachment; filename="layerdownloads-"' + \
        str(datetoday.month) + str(datetoday.day) + \
        str(datetoday.year) + '.csv"'
    listtowrite = []
    writer = csv.writer(response)

    orgtypelist = ['Phil-LiDAR 1 SUC',
                   'Phil-LiDAR 2 SUC',
                   'Government Agency',
                   'Academe',
                   'International NGO',
                   'Local NGO',
                   'Private Insitution',
                   'Other']

    auth_list = DownloadTracker.objects.order_by('timestamp')
    writer.writerow(['username', 'lastname', 'firstname', 'email', 'organization',
                     'organization type', 'purpose', 'layer name', 'date downloaded', 'area', 'size_in_bytes'])

    pprint("writing authenticated downloads list")

    for auth in auth_list:
        username = auth.actor
        getprofile = Profile.objects.get(username=username)
        firstname = unidecode(getprofile.first_name)
        lastname = unidecode(getprofile.last_name)
        email = getprofile.email
        organization = unidecode(
            getprofile.organization) if getprofile.organization is not None else getprofile.organization
        orgtype = getprofile.org_type
        #area = get_area_coverage(auth.action_object.typename)
        area = 0
        # pprint(dir(getprofile))
        if auth.resource_type != 'document':
            listtowrite.append([username, lastname, firstname, email, organization, orgtype,
                                "", auth.title, auth.timestamp.strftime('%Y/%m/%d'), area, ''])
    # writer.writerow(['\n'])
    anon_list = AnonDownloader.objects.all().order_by('date')
    # writer.writerow(['Anonymous Downloads'])
    # writer.writerow( ['lastname','firstname','email','organization','organization type','purpose','layer name','doc name','date downloaded'])

    pprint("writing anonymous downloads list")
    for anon in anon_list:
        lastname = unidecode(anon.anon_last_name)
        firstname = unidecode(anon.anon_first_name)
        email = anon.anon_email
        layername = anon.anon_layer
        docname = anon.anon_document
        organization = unidecode(
            anon.anon_organization) if anon.anon_organization is not None else anon.anon_organization
        orgtype = anon.anon_orgtype
        purpose = unidecode(
            anon.anon_purpose) if anon.anon_purpose is not None else anon.anon_purpose
        #area = get_area_coverage(layername.typename)
        area = 0
        if layername:
            listtowrite.append(["", lastname, firstname, email, organization, orgtype,
                                purpose, layername, anon.date.strftime('%Y/%m/%d'), area, ''])
    listtowrite.sort(key=lambda x: datetime.datetime.strptime(
        x[8], '%Y/%m/%d'), reverse=True)

    pprint("writing ftp downloads list")
    for ftp_request in FTPRequest.objects.all():
        ftp_detail = get_ftp_details(ftp_request)
        username = ftp_detail['user'].username
        lastname = unidecode(ftp_detail['user'].last_name)
        firstname = unidecode(ftp_detail['user'].first_name)
        email = ftp_detail['user'].email
        organization = unidecode(ftp_detail['organization']) if ftp_detail[
            'organization'] is not None else ftp_detail['organization']
        organization_type = ftp_detail['organization_type']
        date_requested = ftp_request.date_time.strftime('%Y/%m/%d')

        listtowrite.append([username, lastname, firstname, email, organization, organization_type, "",
                            "LAZ", date_requested, ftp_detail['number_of_laz'], ftp_detail['size_of_laz']])
        listtowrite.append([username, lastname, firstname, email, organization, organization_type, "",
                            "DSM", date_requested, ftp_detail['number_of_dsm'], ftp_detail['size_of_dsm']])
        listtowrite.append([username, lastname, firstname, email, organization, organization_type, "",
                            "DTM", date_requested, ftp_detail['number_of_dtm'], ftp_detail['size_of_dtm']])
        listtowrite.append([username, lastname, firstname, email, organization, organization_type, "",
                            "Ortho", date_requested, ftp_detail['number_of_ortho'], ftp_detail['size_of_ortho']])

    for eachtowrite in listtowrite:
        writer.writerow(eachtowrite)

    return response
