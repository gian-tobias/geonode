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

from django.conf.urls import include, patterns, url
from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.conf.urls.static import static
from geonode.sitemap import LayerSitemap, MapSitemap
from django.views.generic import TemplateView
from django.contrib import admin

import geonode.proxy.urls
import geonode.cephgeo.urls

from geonode.api.urls import api

import django_cas_ng

import autocomplete_light
from geonode.views import forbidden

#from geonode import settings

# Setup Django Admin
autocomplete_light.autodiscover()

admin.autodiscover()

js_info_dict = {
    'domain': 'djangojs',
    'packages': ('geonode',)
}

sitemaps = {
    "layer": LayerSitemap,
    "map": MapSitemap
}

urlpatterns = patterns('',

                       # Static pages
                       url(r'^/?$', 'geonode.views.philgrid', name='home'),
                       url(r'^help/$', TemplateView.as_view(template_name='help.html'), name='help'),
                       url(r'^developer/$', TemplateView.as_view(template_name='developer.html'), name='developer'),
                       url(r'^about/$', TemplateView.as_view(template_name='about.html'), name='about'),

                       # EULA URLs
                       url(r'^eula/', include('geonode.eula.urls'), name='eula'),

                       # Permission denied handler
                       url(r'^forbidden/$', forbidden, name='forbidden'),

                       # Layer views
                       (r'^layers/', include('geonode.layers.urls')),

                       # Map views
                       (r'^maps/', include('geonode.maps.urls')),

                       # Catalogue views
                       (r'^catalogue/', include('geonode.catalogue.urls')),

                       # data.json
                       url(r'^data.json$', 'geonode.catalogue.views.data_json', name='data_json'),

                       # Search views
                       url(r'^search/$', TemplateView.as_view(template_name='search/search.html'), name='search'),

                       # Social views
                       #(r"^account/", include("account.urls")),
                       (r'^people/', include('geonode.people.urls')),
                       (r'^avatar/', include('avatar.urls')),
                       (r'^comments/', include('dialogos.urls')),
                       (r'^ratings/', include('agon_ratings.urls')),
                       (r'^activity/', include('actstream.urls')),
                       (r'^announcements/', include('announcements.urls')),
                       (r'^messages/', include('user_messages.urls')),
                       (r'^social/', include('geonode.social.urls')),
                       (r'^security/', include('geonode.security.urls')),
                       url(r'^register/$','geonode.datarequests.views.register', name='register'),

                       # Accounts
                       #url(r'^account/ajax_login$', 'geonode.views.ajax_login', name='account_ajax_login'),
                       url(r'^account/login$', 'django_cas_ng.views.login', name='account_ajax_login'),
                       url(r'^account/login/$','django_cas_ng.views.login', name='account_login'),
                       url(r'^account/login/$', 'django_cas_ng.views.login', name='cas_ng_login'),
                       url(r'^account/logout/$', 'django_cas_ng.views.logout', name='cas_ng_logout'),
                       url(r'^account/logout/$', 'django_cas_ng.views.logout', name='account_logout'),
                       url(r'^accounts/callback$', 'django_cas_ng.views.callback', name='cas_ng_proxy_callback'),
                       url(r'^account/ajax_lookup$', 'geonode.views.ajax_lookup', name='account_ajax_lookup'),

		       #Geocoding
                       (r'^geocoding/',include('geonode.arealocate.urls',namespace='arealocate')),

                       # Data Request Profiles
                       # Resolve /requests/register/profile_request, proceed to datarequests/urls.py
                       (r'^requests/', include('geonode.datarequests.urls', namespace='datarequests')),

                       # Misc
                       # url(r'^captcha/', include('captcha.urls')),

					   # CephGeo
					   url(r'^ceph/', include("geonode.cephgeo.urls")),

             # Automation
             url(r'^automation/', include('geonode.automation.urls', namespace='automation')),

					   #MapTiles
					   url(r'^maptiles/',include("geonode.maptiles.urls")),

                       # Meta
                       url(r'^lang\.js$', TemplateView.as_view(template_name='lang.js', content_type='text/javascript'),
                           name='lang'),

                       url(r'^jsi18n/$', 'django.views.i18n.javascript_catalog', js_info_dict, name='jscat'),
                       url(r'^sitemap\.xml$', 'django.contrib.sitemaps.views.sitemap', {'sitemaps': sitemaps},
                           name='sitemap'),

                       (r'^i18n/', include('django.conf.urls.i18n')),
                       (r'^autocomplete/', include('autocomplete_light.urls')),
                       (r'^admin/', include(admin.site.urls)),
                       (r'^groups/', include('geonode.groups.urls')),
                       (r'^documents/', include('geonode.documents.urls')),
                       (r'^services/', include('geonode.services.urls')),
                       url(r'', include(api.urls)),
                       (r'^api/', include('geonode.api.urls')),

                       # Reports views
                       (r'^reports/', include('geonode.reports.urls')),
                       )

if "geonode.contrib.dynamic" in settings.INSTALLED_APPS:
    urlpatterns += patterns('',
                            (r'^dynamic/', include('geonode.contrib.dynamic.urls')),
                            )

if 'geonode.geoserver' in settings.INSTALLED_APPS:
    # GeoServer Helper Views
    urlpatterns += patterns('',
                            # Upload views
                            (r'^upload/', include('geonode.upload.urls')),
                            (r'^gs/', include('geonode.geoserver.urls')),
                            )

if 'notification' in settings.INSTALLED_APPS:
    urlpatterns += patterns('',
                            (r'^notifications/', include('notification.urls')),
                            )

if 'simple_sso.sso_server' in settings.INSTALLED_APPS:
    from simple_sso.sso_server.server import Server
    server = Server()
    urlpatterns += server.get_urls()

# Set up proxy
urlpatterns += geonode.proxy.urls.urlpatterns

# Serve static files
urlpatterns += staticfiles_urlpatterns()
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
handler403 = 'geonode.views.err403'

# Featured Maps Pattens
urlpatterns += patterns('',
                        (r'^featured/(?P<site>[A-Za-z0-9_\-]+)/$', 'geonode.maps.views.featured_map'),
                        (r'^featured/(?P<site>[A-Za-z0-9_\-]+)/info$', 'geonode.maps.views.featured_map_info'),
                        )
