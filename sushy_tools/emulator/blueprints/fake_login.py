import argparse
from flask import Blueprint
from flask import current_app
from flask import jsonify

fake_login = Blueprint('fake_login', __name__)

@fake_login.route('/redfish/v1/SessionService/Sessions', methods=['POST'])
def session_collection_resource():
    current_app.logger.debug('Creating session')
    return jsonify(session_key='dummy_session_key',
            session_location='dummy_session_location')
