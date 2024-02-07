import os
import threading
import serial
import time
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, jsonify, request, session
from flask_cors import CORS
from flask_session import Session
from collections import Counter

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = '31a6d43a34178b9d483370a095e426d2'
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

firebase_creds = {
    "type": os.getenv("FIREBASE_TYPE"),
    "project_id": os.getenv("FIREBASE_PROJECT_ID"),
    "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
    "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
    "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
    "client_id": os.getenv("FIREBASE_CLIENT_ID"),
    "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
    "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL")
}

cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred, {
    'databaseURL': os.getenv('FIREBASE_DATABASE_URL')
})

current_state = None
collected_data = []
calibration_data_global = {}

def retrieve_calibration_data(training_id):
    ref = db.reference(f'users/{session.get("user_id")}/Calibration/Training/{training_id}/Thresholds')
    calibration_data_global = ref.get()
    if calibration_data_global:
        print("Calibration data retrieved successfully.")
        print("Calibration data:", calibration_data_global)
    else:
        print("Failed to retrieve calibration data.")
    return calibration_data_global

def convert_to_category(value, ranges):
    try:
        if not ranges:
            raise ValueError(f"Empty ranges provided for value: {value}")
        if value >= ranges['high'][0] and value <= ranges['high'][1]:
            return 'HIGH'
        elif value >= ranges['medium'][0] and value <= ranges['medium'][1]:
            return 'MEDIUM'
        elif value >= ranges['low'][0] and value <= ranges['low'][1]:
            return 'LOW'
        else:
            return 'NOT ACTIVATED'
    except KeyError as e:
        print(f"KeyError accessing range: {e}, with ranges: {ranges}")
        raise  # Re-raise the exception or handle it as appropriate

def calibration_mode():
    global collected_data, user_id, training_id
    if not collected_data or not all(collected_data):
        print("No data collected.")
        return

    thresholds = {}

    # Define custom sensor names
    sensor_names = {
        'sensor_1': 'R_quads',
        'sensor_2': 'R_hams',
        'sensor_3': 'R_glutes',
        'sensor_4': 'L_quads',
        'sensor_5': 'L_hams',
        'sensor_6': 'L_glutes',
    }

    # Iterate over each sensor's collected data
    for i, sensor_data in enumerate(collected_data):
        sensor_key = f'sensor_{i + 1}'
        if not sensor_data:  # Skip if no data collected for this sensor
            print(f"No data collected for {sensor_names.get(sensor_key, sensor_key)}.")
            continue

        max_value = max(sensor_data)
        activation_threshold = int(round(max_value * 0.10))
        medium_value = int(round(max_value * 2 / 3))

        # Assuming you want to define ranges based on calculated thresholds
        high_range = (medium_value, max_value)

        # mode within the high range
        high_values = [value for data_list in collected_data for value in data_list if
                       high_range[0] <= value <= high_range[1]]
        high_value_mode = Counter(high_values).most_common(1)[0][0] if high_values else None

        # set new high val from mode within high range
        new_high_value = high_value_mode

        # increment for the new range
        increment = int(round((new_high_value - activation_threshold) / 3))

        # new ranges (final)
        not_activated_range = (0, activation_threshold - 1)
        new_low_range = (activation_threshold, activation_threshold + increment)
        new_medium_range = (activation_threshold + increment + 1, activation_threshold + 2 * increment)
        new_high_range = (activation_threshold + 2 * increment + 1, new_high_value)

        # Use custom sensor names
        sensor_name = sensor_names.get(sensor_key, sensor_key)

        # Store the threshold ranges for this sensor with custom sensor names
        thresholds[sensor_name] = {
            'not_activated': not_activated_range,
            'low': new_low_range,
            'medium': new_medium_range,
            'high': new_high_range
        }
    if user_id:
        save_thresholds_to_firebase(user_id, training_id, thresholds)
    else:
        print("No current user ID set.")

def save_thresholds_to_firebase(user_id, training_id, thresholds):
    ref = db.reference(f'/users/{user_id}/Calibration/Training/{training_id}')
    ref.update({
        'Thresholds': thresholds
    })
    print("Thresholds saved to Firebase.")

def collect_data():
    pass  # Placeholder for collecting data

def calculate_and_save_thresholds():
    pass  # Placeholder for calculating and saving thresholds

@app.route('/api/userId', methods=['POST'])
def receive_user_data():
    global user_id, training_id
    data = request.json
    user_id = data.get('userId')
    training_id = data.get('trainingId')

    # Store user_id in session
    session['user_id'] = user_id

    # Call the calibration_mode() function with user_id
    calibration_mode()

    # You can now use session['user_id'] to access the user_id in other routes
    print("Received user ID:", session.get('user_id'))
    print("Received training ID:", training_id)

    # Return a response to the React Native app
    return jsonify({"status": "success", "message": "User data received successfully."})

@app.route('/start-calibration', methods=['POST'])
def start_calibration():
    global current_state, collected_data
    current_state = 1
    collected_data = []  # Reset collected data
    threading.Thread(target=collect_data).start()
    return jsonify({"message": "Calibration started."})

@app.route('/stop-and-process', methods=['POST'])
def stop_and_process():
    global current_state
    if current_state == 1:
        current_state = None
        calculate_and_save_thresholds()  # Process data after collection stops
        return jsonify({"message": "Data processed and saved to Firebase."})
    else:
        return jsonify({"message": "Calibration not in progress."})

@app.route('/reset-timer', methods=['POST'])
def reset_timer():
    global start_time, current_state
    start_time = None  # Or reset as appropriate for your logic
    current_state = None  # Optionally reset the current state
    # Implement any additional reset logic here
    return jsonify({'message': 'Timer and state reset successfully'})

@app.route('/set-state-1', methods=['POST'])
def set_state_1():
    global current_state, collected_data
    current_state = 1
    collected_data = [[] for _ in range(6)]  # Reset collected data for new session
    return jsonify({'message': 'State set to 1, calibration started'})

@app.route('/set-state-2', methods=['POST'])
def set_state_2():
    global current_state
    current_state = 2
    return jsonify({'message': 'State set to 2'})

@app.route('/set-state-3', methods=['POST'])
def set_state_3():
    global current_state, collected_data, calibration_data_global
    data = request.json
    training_id = data.get('trainingType')

    # Retrieve calibration data for the selected training type
    calibration_data_global = retrieve_calibration_data(training_id)

    if not calibration_data_global:
        return jsonify({"message": "Failed to retrieve calibration data or invalid training type."}), 400

    current_state = 3  # Assuming state 4 is for training mode
    collected_data = [[] for _ in range(6)]  # Reset collected data for new session
    return jsonify({'message': 'Training mode started, calibration data retrieved'})

@app.route('/get-state', methods=['GET'])
def get_state():
    return jsonify({'state': current_state})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
