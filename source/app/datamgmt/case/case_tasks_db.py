#!/usr/bin/env python3
#
#  IRIS Source Code
#  Copyright (C) 2021 - Airbus CyberSecurity (SAS)
#  ir@cyberactionlab.net
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

from datetime import datetime
from sqlalchemy import desc, and_

from app import db
from app.datamgmt.manage.manage_attribute_db import get_default_custom_attributes
from app.datamgmt.states import update_tasks_state
from app.models import CaseTasks, TaskAssignee
from app.models import TaskStatus
from app.models.authorization import User


def get_tasks_status():
    return TaskStatus.query.all()


def get_tasks(caseid):
    return CaseTasks.query.with_entities(
        CaseTasks.id.label("task_id"),
        CaseTasks.task_title,
        CaseTasks.task_description,
        CaseTasks.task_open_date,
        CaseTasks.task_tags,
        CaseTasks.task_status_id,
        TaskStatus.status_name,
        TaskStatus.status_bscolor
    ).filter(
        CaseTasks.task_case_id == caseid
    ).join(
        CaseTasks.status
    ).order_by(
        desc(TaskStatus.status_name)
    ).all()


def get_task(task_id, caseid):
    return CaseTasks.query.filter(CaseTasks.id == task_id, CaseTasks.task_case_id == caseid).first()


def get_task_with_assignees(task_id, case_id):
    task = get_task(task_id, case_id)
    if not task:
        return None

    get_assignee_list = TaskAssignee.query.with_entities(
        TaskAssignee.task_id,
        User.user,
        User.id,
        User.name
    ).join(
        TaskAssignee.user
    ).filter(
        TaskAssignee.task_id == task_id
    ).all()

    membership_list = {}
    for member in get_assignee_list:
        if member.task_id not in membership_list:

            membership_list[member.task_id] = [{
                'user': member.user,
                'name': member.name,
                'id': member.id
            }]
        else:
            membership_list[member.task_id].append({
                'user': member.user,
                'name': member.name,
                'id': member.id
            })

    setattr(task, 'task_assignees', membership_list.get(task.id, []))

    return task


def update_task_status(task_status, task_id, caseid):
    task = get_task(task_id, caseid)
    if task:
        try:
            task.task_status_id = task_status

            update_tasks_state(caseid=caseid)
            db.session.commit()
            return True

        except:
            return False
    else:
        return False


def update_task_assignees(task, task_assignee_list):
    if not task:
        return None

    cur_assignee_list = TaskAssignee.query.with_entities(
        TaskAssignee.user_id
    ).filter(TaskAssignee.task_id == task.id).all()

    # Some formatting
    set_cur_assignees = set([assignee[0] for assignee in cur_assignee_list])
    set_assignees = set(int(assignee) for assignee in task_assignee_list)

    assignees_to_add = set_assignees - set_cur_assignees
    assignees_to_remove = set_cur_assignees - set_assignees

    for uid in assignees_to_add:
        user = User.query.filter(User.id == uid).first()
        if user:
            ta = TaskAssignee()
            ta.task_id = task.id
            ta.user_id = user.id
            db.session.add(ta)

    for uid in assignees_to_remove:
        TaskAssignee.query.filter(
            and_(TaskAssignee.task_id == task.id,
                 TaskAssignee.user_id == uid)
        ).delete()

    db.session.commit()

    return task


def add_task(task, assignee_id_list, user_id, caseid):
    now = datetime.now()
    task.task_case_id = caseid
    task.task_userid_open = user_id
    task.task_userid_update = user_id
    task.task_open_date = now
    task.task_last_update = now

    task.custom_attributes = task.custom_attributes if task.custom_attributes else get_default_custom_attributes('task')

    db.session.add(task)

    update_tasks_state(caseid=caseid)
    db.session.commit()

    update_task_status(task.task_status_id, task.id, caseid)
    update_task_assignees(task, assignee_id_list)

    return task
