import sys
import re
import urllib
import json

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.http import HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import _get_queryset

from catmaid.models import Project, UserRole
from django.contrib.auth.models import User

from catmaid.control.common import json_error_response, cursor_fetch_dictionary
from catmaid.control.common import my_render_to_response

from django.contrib.auth import authenticate, logout, login

from guardian.models import UserObjectPermission, GroupObjectPermission
from guardian.shortcuts import get_perms_for_model, get_objects_for_user, get_perms, get_objects_for_group
from functools import wraps
from itertools import groupby

def login_vnc(request):
    return my_render_to_response(request,
                                 'vncbrowser/login.html',
                                {'return_url': request.GET.get('return_url', '/'),
                                 'project_id': 0,
                                 'catmaid_url': settings.CATMAID_URL,
                                 'catmaid_login': settings.CATMAID_URL + 'model/login.php'})


def login_user(request):
    profile_context = {}
    if request.method == 'POST':
        # Try to log the user into the system.
        username = request.POST.get('name', 0)
        password = request.POST.get('pwd', 0)
        user = authenticate(username=username, password=password)

        if user is not None:
            profile_context['userprofile'] = user.userprofile.as_dict()
            if user.is_active:
                # Redirect to a success page.
                request.session['user_id'] = user.id
                login(request, user)
                # Add some context information
                profile_context['id'] = request.session.session_key
                profile_context['longname'] = user.get_full_name()
                return HttpResponse(json.dumps(profile_context))
            else:
               # Return a 'disabled account' error message
               profile_context['error'] = ' Disabled account'
               return HttpResponse(json.dumps(profile_context))
        else:
            # Return an 'invalid login' error message.
            profile_context['userprofile'] = request.user.userprofile.as_dict()
            profile_context['error'] = ' Invalid login'
            return HttpResponse(json.dumps(profile_context))
    else:   # request.method == 'GET'
        profile_context['userprofile'] = request.user.userprofile.as_dict()
        # Check if the user is logged in.
        if request.user.is_authenticated():
            profile_context['id'] = request.session.session_key
            profile_context['longname'] = request.user.get_full_name()
            return HttpResponse(json.dumps(profile_context))
        else:
            # Return a 'not logged in' warning message.
            profile_context['warning'] = ' Not logged in'
            return HttpResponse(json.dumps(profile_context))


def logout_user(request):
    logout(request)
    # Return profile context of anonymous user
    anon_user = User.objects.get(id=settings.ANONYMOUS_USER_ID)
    profile_context = {}
    profile_context['userprofile'] = anon_user.userprofile.as_dict()
    profile_context['success'] = True
    return HttpResponse(json.dumps(profile_context))


def requires_user_role(roles):
    """
    This decorator will return a JSON error response unless the user is logged in 
    and has at least one of the indicated roles or admin role for the project.
    """
    
    def decorated_with_requires_user_role(f):
        def inner_decorator(request, roles=roles, *args, **kwargs):
            p = Project.objects.get(pk=kwargs['project_id'])
            u = request.user
            
            # Check for admin privs in all cases.
            has_role = u.has_perm('can_administer', p)
            
            if not has_role:
                # Check the indicated role(s)
                if isinstance(roles, str):
                    roles = [roles]
                for role in roles:
                    if role == UserRole.Annotate:
                        has_role = u.has_perm('can_annotate', p)
                    elif role == UserRole.Browse:
                        has_role = u.has_perm('can_browse', p)
                    if has_role:
                        break
            
            if has_role:
                # The user can execute the function.
                return f(request, *args, **kwargs)
            else:
                return json_error_response("The user '%s' does not have a necessary role in the project %d" % (u.first_name + ' ' + u.last_name, int(kwargs['project_id'])))
            
        return wraps(f)(inner_decorator)
    return decorated_with_requires_user_role

def get_objects_and_perms_for_user(user, codenames, klass, use_groups=True, any_perm=False):
    """ Similar to what guardian's get_objects_for_user method does,
    this method return a dictionary of object IDs (!) of model klass
    objects with the permissions the user has on them associated.
    These permissions may result from explicit user permissions or
    implicit group ones. Parts of the source code are takes from
    django-guardian. Note, that there an object list is returned. In
    contrast this method returns object IDs and the permissions on
    the actual objects.
    """
    # Get QuerySet on and ContentType of the model
    queryset = _get_queryset(klass)
    ctype = ContentType.objects.get_for_model(queryset.model)

    # A super user has all permissions available on all the objects
    # of a model.
    if user.is_superuser:
        # Get all permissions for the model
        perms = get_perms_for_model(klass)
        permNames = set(perm.codename for perm in perms)
        pk_dict = {}
        for p in queryset:
            pk_dict[p.id] = permNames
        return pk_dict

    # Extract a list of tuples that contain an object's primary
    # key and a permission codename that the user has on them.
    user_obj_perms = UserObjectPermission.objects\
        .filter(user=user)\
        .filter(permission__content_type=ctype)\
        .filter(permission__codename__in=codenames)\
        .values_list('object_pk', 'permission__codename')
    data = list(user_obj_perms)
    if use_groups:
        groups_obj_perms = GroupObjectPermission.objects\
            .filter(group__user=user)\
            .filter(permission__content_type=ctype)\
            .filter(permission__codename__in=codenames)\
            .values_list('object_pk', 'permission__codename')
        data += list(groups_obj_perms)
    # sorting/grouping by pk (first in result tuple)
    keyfunc = lambda t: int(t[0])
    data = sorted(data, key=keyfunc)

    # Create the result dictionary, associating one object id
    # with a set of permissions the user has for it.
    pk_dict = {}
    for pk, group in groupby(data, keyfunc):
        obj_codenames = set((e[1] for e in group))
        if any_perm or codenames.issubset(obj_codenames):
            pk_dict[pk] = obj_codenames

    return pk_dict

def user_project_permissions(request):
    """ If a user is authenticated, this method returns a dictionary
    that stores whether the user has a specific permission on a project.
    If a user is not authenticated, this dictionary will be empty.
    """
    result = {}
    if request.user.is_authenticated():
        projectPerms = get_perms_for_model(Project)
        permNames = [perm.codename for perm in projectPerms]
        # Find out what permissions a user actually has for any of those projects.
        projects = get_objects_and_perms_for_user(request.user, permNames,
                                                  Project, any_perm = True)
        # Build the result data structure
        for project_id in projects:
            userPerms = projects[project_id]
            # Iterate the codenames of available permissions and store
            # whether the user has them for a specific project
            for permName in permNames:
                if permName not in result:
                    result[permName] = {}
                result[permName][project_id] = permName in userPerms

    return HttpResponse(json.dumps(result))


def can_edit_or_fail(user, ob_id, table_name):
    """ Returns true if the user owns the object or if the user is a superuser.
    Raises an Exception if the user cannot edit the object
    or if the object does not exist.
    Expects the ob_id to be an integer. """
    # Sanitize arguments -- can't give them to django to sanitize,
    # for django will quote the table name
    ob_id = int(ob_id)
    if not re.match('^[a-z_]+$', table_name):
        raise Exception('Invalid table name: %s' % table_name)

    cursor = connection.cursor()
    cursor.execute("SELECT user_id FROM %s WHERE id=%s" % (table_name, ob_id))
    rows = tuple(cursor.fetchall())
    if rows:
        if user.is_superuser:
            return True
        if 1 == len(rows) and rows[0][0] == user.id:
            return True
        raise Exception('User %s with id #%s cannot edit object #%s (from user #%s) from table %s' % (user.username, user.id, ob_id, rows[0][0], table_name))
    raise ObjectDoesNotExist('Object #%s not found in table %s' % (ob_id, table_name))

def can_edit_all_or_fail(user, ob_ids, table_name):
    """ Returns true if the user owns all the objects or if the user is a superuser.
    Raises an Exception if the user cannot edit the object
    or if the object does not exist."""
    # Sanitize arguments -- can't give them to django to sanitize,
    # for django will quote the table name
    ob_ids = set(ob_ids)
    str_ob_ids = ','.join(str(int(x)) for x in ob_ids)
    if not re.match('^[a-z_]+$', table_name):
        raise Exception('Invalid table name: %s' % table_name)

    cursor = connection.cursor()
    cursor.execute("SELECT user_id, count(user_id) FROM %s WHERE id IN (%s) GROUP BY user_id" % (table_name, str_ob_ids))
    rows = tuple(cursor.fetchall())
    if rows and len(ob_ids) == sum(row[1] for row in rows):
        if user.is_superuser:
            return True
        if 1 == len(rows) and rows[0][0] == user.id:
            return True
        raise Exception('User %s cannot edit all of the %s unique objects from table %s' % (user.username, len(ob_ids), table_name))
    raise ObjectDoesNotExist('One or more of the %s unique objects were not found in table %s' % (len(ob_ids), table_name))


@requires_user_role([UserRole.Annotate])
def all_usernames(request, project_id=None):
    """ Return an ordered list of all usernames, each entry a list of id and username. """
    cursor = connection.cursor()
    cursor.execute('''
    SELECT id, username FROM auth_user WHERE id != -1 ORDER BY username DESC
    ''')
    return HttpResponse(json.dumps(cursor.fetchall()))
 
