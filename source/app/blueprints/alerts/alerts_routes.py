#!/usr/bin/env python3
#
#  IRIS Source Code
#  Copyright (C) 2023 - DFIR-IRIS
#  contact@dfir-iris.org
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
import json

import marshmallow
from flask_wtf import FlaskForm
from typing import Union, List

from datetime import datetime

from flask import Blueprint, request, render_template, redirect, url_for
from flask_login import current_user
from werkzeug import Response

import app
from app import db
from app.blueprints.case.case_comments import case_comment_update
from app.datamgmt.alerts.alerts_db import get_filtered_alerts, get_alert_by_id, create_case_from_alert, \
    merge_alert_in_case, unmerge_alert_from_case, cache_similar_alert, get_related_alerts, get_related_alerts_details, \
    get_alert_comments, delete_alert_comment, get_alert_comment, delete_similar_alert_cache, delete_alerts, \
    create_case_from_alerts
from app.datamgmt.case.case_db import get_case
from app.iris_engine.access_control.utils import ac_set_new_case_access
from app.iris_engine.module_handler.module_handler import call_modules_hook
from app.iris_engine.utils.tracker import track_activity
from app.models import Ioc, CaseAssets
from app.models.alerts import AlertStatus
from app.models.authorization import Permissions
from app.schema.marshables import AlertSchema, CaseSchema, CommentSchema, CaseAssetsSchema, IocSchema
from app.util import ac_api_requires, response_error, str_to_bool, add_obj_history_entry, ac_requires
from app.util import response_success

alerts_blueprint = Blueprint(
    'alerts',
    __name__,
    template_folder='templates'
)


# CONTENT ------------------------------------------------
@alerts_blueprint.route('/alerts/filter', methods=['GET'])
@ac_api_requires(Permissions.alerts_read, no_cid_required=True)
def alerts_list_route(caseid) -> Response:
    """
    Get a list of alerts from the database

    args:
        caseid (str): The case id

    returns:
        Response: The response
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)

    alert_ids_str = request.args.get('alert_ids')
    if alert_ids_str:
        try:

            if ',' in alert_ids_str:
                alert_ids = [int(alert_id) for alert_id in alert_ids_str.split(',')]

            else:
                alert_ids = [int(alert_ids_str)]

        except ValueError:
            return response_error('Invalid alert id')

    else:
        alert_ids = None

    alert_schema = AlertSchema()

    filtered_data = get_filtered_alerts(
        start_date=request.args.get('source_start_date'),
        end_date=request.args.get('source_end_date'),
        title=request.args.get('alert_title'),
        description=request.args.get('alert_description'),
        status=request.args.get('alert_status_id', type=int),
        severity=request.args.get('alert_severity_id', type=int),
        owner=request.args.get('alert_owner_id', type=int),
        source=request.args.get('alert_source'),
        tags=request.args.get('alert_tags'),
        classification=request.args.get('alert_classification_id', type=int),
        client=request.args.get('alert_customer_id'),
        case_id=request.args.get('case_id', type=int),
        alert_ids=alert_ids,
        page=page,
        per_page=per_page,
        sort=request.args.get('sort')
    )

    if filtered_data is None:
        return response_error('Filtering error')

    alerts = {
        'total': filtered_data.total,
        'alerts': alert_schema.dump(filtered_data.items, many=True),
        'last_page': filtered_data.pages,
        'current_page': filtered_data.page,
        'next_page': filtered_data.next_num if filtered_data.has_next else None,
    }

    return response_success(data=alerts)


@alerts_blueprint.route('/alerts/add', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_add_route(caseid) -> Response:
    """
    Add a new alert to the database

    args:
        caseid (str): The case id

    returns:
        Response: The response
    """

    if not request.json:
        return response_error('No JSON data provided')

    alert_schema = AlertSchema()
    ioc_schema = IocSchema()
    asset_schema = CaseAssetsSchema()

    try:
        # Load the JSON data from the request
        data = request.get_json()

        iocs_list = data.pop('alert_iocs', [])
        assets_list = data.pop('alert_assets', [])

        iocs = ioc_schema.load(iocs_list, many=True)
        assets = asset_schema.load(assets_list, many=True)

        # Deserialize the JSON data into an Alert object
        new_alert = alert_schema.load(data)

        new_alert.alert_creation_time = datetime.utcnow()

        new_alert.iocs = iocs
        new_alert.assets = assets

        # Add the new alert to the session and commit it
        db.session.add(new_alert)
        db.session.commit()

        # Add history entry
        add_obj_history_entry(new_alert, 'Alert created')

        # Cache the alert for similarities check
        cache_similar_alert(new_alert.alert_customer_id, assets=assets_list,
                            iocs=iocs_list, alert_id=new_alert.alert_id)

        track_activity(f"created alert #{new_alert.alert_id} - {new_alert.alert_title}")

        # Emit a socket io event
        app.socket_io.emit('new_alert', json.dumps({
            'alert_id': new_alert.alert_id,
            'alert_title': new_alert.alert_title
        }), namespace='/alerts')

        # Return the newly created alert as JSON
        return response_success(data=alert_schema.dump(new_alert))

    except Exception as e:
        app.app.logger.exception(e)
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/<int:alert_id>', methods=['GET'])
@ac_api_requires(Permissions.alerts_read, no_cid_required=True)
def alerts_get_route(caseid, alert_id) -> Response:
    """
    Get an alert from the database

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """

    alert_schema = AlertSchema()

    # Get the alert from the database
    alert = get_alert_by_id(alert_id)

    # Return the alert as JSON
    if alert is None:
        return response_error('Alert not found')

    alert_dump = alert_schema.dump(alert)

    # Get similar alerts
    similar_alerts = get_related_alerts(alert.alert_customer_id, alert.assets, alert.iocs)
    alert_dump['related_alerts'] = similar_alerts

    return response_success(data=alert_dump)


@alerts_blueprint.route('/alerts/similarities/<int:alert_id>', methods=['GET'])
@ac_api_requires(Permissions.alerts_read, no_cid_required=True)
def alerts_similarities_route(caseid, alert_id) -> Response:
    """
    Get an alert and similarities from the database

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """

    # Get the alert from the database
    alert = get_alert_by_id(alert_id)

    # Return the alert as JSON
    if alert is None:
        return response_error('Alert not found')

    open_alerts = request.args.get('open-alerts', 'false').lower() == 'true'
    open_cases = request.args.get('open-cases', 'false').lower() == 'true'
    closed_cases = request.args.get('closed-cases', 'false').lower() == 'true'
    closed_alerts = request.args.get('closed-alerts', 'false').lower() == 'true'

    # Get similar alerts
    similar_alerts = get_related_alerts_details(alert.alert_customer_id, alert.assets, alert.iocs,
                                                open_alerts=open_alerts, open_cases=open_cases,
                                                closed_cases=closed_cases, closed_alerts=closed_alerts)

    return response_success(data=similar_alerts)


@alerts_blueprint.route('/alerts/update/<int:alert_id>', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_update_route(alert_id, caseid) -> Response:
    """
    Update an alert in the database

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """
    if not request.json:
        return response_error('No JSON data provided')

    alert = get_alert_by_id(alert_id)
    if not alert:
        return response_error('Alert not found')

    alert_schema = AlertSchema()

    try:
        # Load the JSON data from the request
        data = request.get_json()

        activity_data = []
        for key, value in data.items():
            old_value = getattr(alert, key, None)
            if old_value is not None and old_value != value:
                activity_data.append(f"\"{key}\" from \"{old_value}\" to \"{value}\"")

        if activity_data:
            track_activity(f"updated alert #{alert_id}: {','.join(activity_data)}", caseid)

        # Deserialize the JSON data into an Alert object
        updated_alert = alert_schema.load(data, instance=alert, partial=True)

        # Save the changes
        db.session.commit()

        # Return the updated alert as JSON
        return response_success(data=alert_schema.dump(updated_alert))

    except Exception as e:
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/batch/update', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_batch_update_route(caseid: int) -> Response:
    """
    Update multiple alerts in the database

    args:
        caseid (int): The case id

    returns:
        Response: The response
    """
    if not request.json:
        return response_error('No JSON data provided')

    # Load the JSON data from the request
    data = request.get_json()

    # Get the list of alert IDs and updates from the request data
    alert_ids: List[int] = data.get('alert_ids', [])
    updates = data.get('updates', {})

    if not alert_ids:
        return response_error('No alert IDs provided')

    alert_schema = AlertSchema()

    # Process each alert ID
    for alert_id in alert_ids:
        alert = get_alert_by_id(alert_id)
        if not alert:
            return response_error(f'Alert with ID {alert_id} not found')

        try:
            activity_data = []
            for key, value in updates.items():
                old_value = getattr(alert, key, None)
                if old_value is not None and old_value != value:
                    activity_data.append(f"\"{key}\" from \"{old_value}\" to \"{value}\"")

            # Deserialize the JSON data into an Alert object
            alert_schema.load(updates, instance=alert, partial=True)

            if activity_data:
                track_activity(f"updated alerts #{alert_id}: {','.join(activity_data)}", caseid)

            # Save the changes
            db.session.commit()

        except Exception as e:
            # Handle any errors during deserialization or DB operations
            return response_error(str(e))

    # Return a success response
    return response_success(msg='Batch update successful')


@alerts_blueprint.route('/alerts/batch/delete', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_batch_delete_route(caseid: int) -> Response:
    """
    Delete multiple alerts from the database

    args:
        caseid (int): The case id

    returns:
        Response: The response
    """
    if not request.json:
        return response_error('No JSON data provided')

    # Load the JSON data from the request
    data = request.get_json()

    # Get the list of alert IDs and updates from the request data
    alert_ids: List[int] = data.get('alert_ids', [])

    if not alert_ids:
        return response_error('No alert IDs provided')

    success, logs = delete_alerts(alert_ids)

    if not success:
        return response_error(logs)

    track_activity(f"deleted alerts #{','.join(str(alert_id) for alert_id in alert_ids)}", caseid)

    return response_success(msg='Batch delete successful')


@alerts_blueprint.route('/alerts/delete/<int:alert_id>', methods=['POST'])
@ac_api_requires(Permissions.alerts_delete, no_cid_required=True)
def alerts_delete_route(alert_id, caseid) -> Response:
    """
    Delete an alert from the database

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """

    alert = get_alert_by_id(alert_id)
    if not alert:
        return response_error('Alert not found')

    try:
        # Delete the case association
        delete_similar_alert_cache(alert_id=alert_id)

        # Delete the alert from the database
        db.session.delete(alert)
        db.session.commit()

        # Return the deleted alert as JSON
        return response_success(data={'alert_id': alert_id})

    except Exception as e:
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/escalate/<int:alert_id>', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_escalate_route(alert_id, caseid) -> Response:
    """
    Escalate an alert

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """

    alert = get_alert_by_id(alert_id)
    if not alert:
        return response_error('Alert not found')

    if request.json is None:
        return response_error('No JSON data provided')

    data = request.get_json()

    iocs_import_list: List[str] = data.get('iocs_import_list')
    assets_import_list: List[str] = data.get('assets_import_list')
    note: str = data.get('note')
    import_as_event: bool = data.get('import_as_event')
    case_tags: str = data.get('case_tags')
    case_title: str = data.get('case_title')


    try:
        # Escalate the alert to a case
        alert.alert_status_id = AlertStatus.query.filter_by(status_name='Escalated').first().status_id
        db.session.commit()

        # Create a new case from the alert
        case = create_case_from_alert(alert, iocs_list=iocs_import_list, assets_list=assets_import_list, note=note,
                                      import_as_event=import_as_event, case_tags=case_tags, case_title=case_title)

        if not case:
            return response_error('Failed to create case from alert')

        ac_set_new_case_access(None, case.case_id)

        case = call_modules_hook('on_postload_case_create', data=case, caseid=caseid)

        add_obj_history_entry(case, 'created')
        track_activity("new case {case_name} created from alert".format(case_name=case.name),
                       caseid=caseid, ctx_less=True)

        # Return the updated alert as JSON
        return response_success(data=CaseSchema().dump(case))

    except Exception as e:

        app.logger.exception(e)

        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/merge/<int:alert_id>', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_merge_route(alert_id, caseid) -> Response:
    """
    Merge an alert into a case

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """
    if request.json is None:
        return response_error('No JSON data provided')

    data = request.get_json()

    target_case_id = data.get('target_case_id')
    if target_case_id is None:
        return response_error('No target case id provided')

    alert = get_alert_by_id(alert_id)
    if not alert:
        return response_error('Alert not found')

    case = get_case(target_case_id)
    if not case:
        return response_error('Target case not found')

    iocs_import_list: List[str] = data.get('iocs_import_list')
    assets_import_list: List[str] = data.get('assets_import_list')
    note: str = data.get('note')
    import_as_event: bool = data.get('import_as_event')
    case_tags = data.get('case_tags')

    try:
        # Merge the alert into a case
        alert.alert_status_id = AlertStatus.query.filter_by(status_name='Merged').first().status_id
        db.session.commit()

        # Merge alert in the case
        merge_alert_in_case(alert, case,
                            iocs_list=iocs_import_list, assets_list=assets_import_list, note=note,
                            import_as_event=import_as_event, case_tags=case_tags)

        # Return the updated alert as JSON
        return response_success(data=CaseSchema().dump(case))

    except Exception as e:
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/unmerge/<int:alert_id>', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_unmerge_route(alert_id, caseid) -> Response:
    """
    Unmerge an alert from a case

    args:
        caseid (str): The case id
        alert_id (int): The alert id

    returns:
        Response: The response
    """
    if request.json is None:
        return response_error('No JSON data provided')

    target_case_id = request.json.get('target_case_id')
    if target_case_id is None:
        return response_error('No target case id provided')

    alert = get_alert_by_id(alert_id)
    if not alert:
        return response_error('Alert not found')

    case = get_case(target_case_id)
    if not case:
        return response_error('Target case not found')

    try:
        # Unmerge alert from the case
        success, message = unmerge_alert_from_case(alert, case)

        if success is False:
            return response_error(message)

        # Return the updated case as JSON
        return response_success(data=AlertSchema().dump(alert), msg=message)

    except Exception as e:
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/batch/merge', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_batch_merge_route(caseid) -> Response:
    """
    Merge multiple alerts into a case

    args:
        caseid (str): The case id

    returns:
        Response: The response
    """
    if request.json is None:
        return response_error('No JSON data provided')

    data = request.get_json()

    target_case_id = data.get('target_case_id')
    if target_case_id is None:
        return response_error('No target case id provided')

    alert_ids = data.get('alert_ids')
    if not alert_ids:
        return response_error('No alert ids provided')

    case = get_case(target_case_id)
    if not case:
        return response_error('Target case not found')

    iocs_import_list: List[str] = data.get('iocs_import_list')
    assets_import_list: List[str] = data.get('assets_import_list')
    note: str = data.get('note')
    import_as_event: bool = data.get('import_as_event')
    case_tags = data.get('case_tags')
    case_title = data.get('case_title')

    try:
        # Merge the alerts into a case
        for alert_id in alert_ids.split(','):
            alert_id = int(alert_id)

            alert = get_alert_by_id(alert_id)
            if not alert:
                continue

            alert.alert_status_id = AlertStatus.query.filter_by(status_name='Merged').first().status_id
            db.session.commit()

            # Merge alert in the case
            merge_alert_in_case(alert, case, iocs_list=iocs_import_list, assets_list=assets_import_list, note=None,
                                import_as_event=import_as_event, case_tags=case_tags)

        if note:
            case.description += f"\n\n### Escalation note\n\n{note}\n\n" if case.description else f"\n\n{note}\n\n"
            db.session.commit()

        # Return the updated case as JSON
        return response_success(data=CaseSchema().dump(case))

    except Exception as e:
        app.app.logger.exception(e)
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts/batch/escalate', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alerts_batch_escalate_route(caseid) -> Response:
    """
    Escalate multiple alerts into a case

    args:
        caseid (str): The case id

    returns:
        Response: The response
    """
    if request.json is None:
        return response_error('No JSON data provided')

    data = request.get_json()

    alert_ids = data.get('alert_ids')
    if not alert_ids:
        return response_error('No alert ids provided')

    iocs_import_list: List[str] = data.get('iocs_import_list')
    assets_import_list: List[str] = data.get('assets_import_list')
    note: str = data.get('note')
    import_as_event: bool = data.get('import_as_event')
    case_tags = data.get('case_tags')
    case_title = data.get('case_title')
    alerts_list = []
    try:
        # Merge the alerts into a case
        for alert_id in alert_ids.split(','):
            alert_id = int(alert_id)

            alert = get_alert_by_id(alert_id)
            if not alert:
                continue

            alert.alert_status_id = AlertStatus.query.filter_by(status_name='Merged').first().status_id
            db.session.commit()
            alerts_list.append(alert)

        # Merge alerts in the case
        case = create_case_from_alerts(alerts_list, iocs_list=iocs_import_list, assets_list=assets_import_list,
                                       note=note, import_as_event=import_as_event, case_tags=case_tags,
                                       case_title=case_title)

        if not case:
            return response_error('Failed to create case from alert')

        ac_set_new_case_access(None, case.case_id)

        case = call_modules_hook('on_postload_case_create', data=case, caseid=caseid)

        add_obj_history_entry(case, 'created')
        track_activity("new case {case_name} created from alerts".format(case_name=case.name),
                       caseid=caseid, ctx_less=True)

        # Return the updated case as JSON
        return response_success(data=CaseSchema().dump(case))

    except Exception as e:
        app.app.logger.exception(e)
        # Handle any errors during deserialization or DB operations
        return response_error(str(e))


@alerts_blueprint.route('/alerts', methods=['GET'])
@ac_requires(Permissions.alerts_read, no_cid_required=True)
def alerts_list_view_route(caseid, url_redir) -> Union[str, Response]:
    """
    List all alerts

    args:
        caseid (str): The case id

    returns:
        Response: The response
    """
    if url_redir:
        return redirect(url_for('alerts.alerts_list_view_route', cid=caseid))

    form = FlaskForm()

    return render_template('alerts.html', caseid=caseid, form=form)


@alerts_blueprint.route('/alerts/<int:cur_id>/comments/modal', methods=['GET'])
@ac_requires(Permissions.alerts_read, no_cid_required=True)
def alert_comment_modal(cur_id, caseid, url_redir):
    """
    Get the modal for the alert comments

    args:
        cur_id (int): The alert id
        caseid (str): The case id

    returns:
        Response: The response
    """
    if url_redir:
        return redirect(url_for('alerts.alerts_list_view_route', cid=caseid, redirect=True))

    alert = get_alert_by_id(cur_id)
    if not alert:
        return response_error('Invalid alert ID')

    return render_template("modal_conversation.html", element_id=cur_id, element_type='alerts',
                           title=f" alert #{alert.alert_id}")


@alerts_blueprint.route('/alerts/<int:alert_id>/comments/list', methods=['GET'])
@ac_api_requires(Permissions.alerts_read, no_cid_required=True)
def alert_comments_get(alert_id, caseid):
    """
    Get the comments for an alert

    args:
        alert_id (int): The alert id
        caseid (str): The case id

    returns:
        Response: The response
    """

    alert_comments = get_alert_comments(alert_id)
    if alert_comments is None:
        return response_error('Invalid alert ID')

    return response_success(data=CommentSchema(many=True).dump(alert_comments))


@alerts_blueprint.route('/alerts/<int:alert_id>/comments/<int:com_id>/delete', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alert_comment_delete(alert_id, com_id, caseid):
    """
    Delete a comment for an alert

    args:
        alert_id (int): The alert id
        com_id (int): The comment id
        caseid (str): The case id

    returns:
        Response: The response
    """

    success, msg = delete_alert_comment(comment_id=com_id, alert_id=alert_id)
    if not success:
        return response_error(msg)

    call_modules_hook('on_postload_alert_comment_delete', data=com_id, caseid=caseid)

    track_activity(f"comment {com_id} on alert {alert_id} deleted", caseid=caseid)
    return response_success(msg)


@alerts_blueprint.route('/alerts/<int:cur_id>/comments/<int:com_id>', methods=['GET'])
@ac_api_requires(Permissions.alerts_read, no_cid_required=True)
def alert_comment_get(cur_id, com_id, caseid):
    """
    Get a comment for an alert

    args:
        cur_id (int): The alert id
        com_id (int): The comment id
        caseid (str): The case id

    returns:
        Response: The response
    """

    comment = get_alert_comment(cur_id, com_id)
    if not comment:
        return response_error("Invalid comment ID")

    return response_success(data=CommentSchema().dump(comment))


@alerts_blueprint.route('/alerts/<int:alert_id>/comments/<int:com_id>/edit', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def alert_comment_edit(alert_id, com_id, caseid):
    """
    Edit a comment for an alert

    args:
        alert_id (int): The alert id
        com_id (int): The comment id
        caseid (str): The case id

    returns:
        Response: The response
    """

    return case_comment_update(com_id, 'events', caseid=None)


@alerts_blueprint.route('/alerts/<int:alert_id>/comments/add', methods=['POST'])
@ac_api_requires(Permissions.alerts_write, no_cid_required=True)
def case_comment_add(alert_id, caseid):
    """
    Add a comment to an alert

    args:
        alert_id (int): The alert id
        caseid (str): The case id

    returns:
        Response: The response
    """

    try:
        alert = get_alert_by_id(alert_id=alert_id)
        if not alert:
            return response_error('Invalid alert ID')

        comment_schema = CommentSchema()

        comment = comment_schema.load(request.get_json())
        comment.comment_alert_id = alert_id
        comment.comment_user_id = current_user.id
        comment.comment_date = datetime.now()
        comment.comment_update_date = datetime.now()
        db.session.add(comment)
        db.session.commit()

        add_obj_history_entry(alert, 'commented')

        db.session.commit()

        hook_data = {
            "comment": comment_schema.dump(comment),
            "alert": AlertSchema().dump(alert)
        }
        call_modules_hook('on_postload_alert_commented', data=hook_data, caseid=caseid)

        track_activity(f"alert \"{alert.alert_id}\" commented", caseid=caseid)
        return response_success("Alert commented", data=comment_schema.dump(comment))

    except marshmallow.exceptions.ValidationError as e:
        return response_error(msg="Data error", data=e.normalized_messages(), status=400)
