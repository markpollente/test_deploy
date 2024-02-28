from flask import Flask, jsonify, request, session
from flask_cors import CORS
import threading
import time
import firebase_admin
from firebase_admin import credentials, db
from flask_session import Session
from collections import Counter
import logging
from scipy.signal import welch
import os
#import json
import numpy as np
#hi

#import random  # Only if you need to simulate data collection
app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = '31a6d43a34178b9d483370a095e426d2'  # Replace with a secure secret key
app.config['SESSION_TYPE'] = 'filesystem'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
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

# Convert the JSON string back into a dictionary
#firebase_creds = json.loads(firebase_creds_json)

# Now pass this dictionary to credentials.Certificate()
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred, {
    'databaseURL': os.getenv('FIREBASE_DATABASE_URL')
})

# Global variables
thresholds = {}
start_time_dict = {}
current_state = None
bluetooth_thread = None
start_time = None
bluetooth_connected = False
bluetooth_serial = None
connection_lock = threading.Lock()
collected_data = []  # To store data collected during c@app.route('/receive-data', methods=['POST'])

# Sensor names should be consistent and defined globally
SENSOR_NAMES = ['R_quads', 'R_hams', 'R_glutes', 'L_quads', 'L_hams', 'L_glutes']

def bluetooth_communication():
    global current_state, collected_data, bluetooth_connected
    start_time = None

    try:
        while bluetooth_connected:
            if current_state in [1, 3]:
                if current_state == 1:
                    if not start_time:  # Start timer when entering state 1
                        start_time = time.time()
                    elif time.time() - start_time > 30:
                        # After 10 seconds in state 1, stop collecting data
                        current_state = None  # Or transition to a different state as needed
                        collected_data = []  # Optionally clear collected data
                        start_time = None  # Reset start time for next session
                        continue  # Skip the rest of the loop to stop data collection

                # Collect data if in state 1 (within 10 seconds) or state 3
                if collected_data:
                    # Process collected_data here
                    pass

            else:
                start_time = None  # Ensure timer is reset when not in state 1

            time.sleep(1)  # Reduce CPU usage, adjust as necessary for responsiveness

    except Exception as e:
        # Handle exceptions
        print(f"Error: {e}")
        bluetooth_connected = False
    finally:
        # Cleanup or final actions when stopping communication
        pass

def retrieve_calibration_data(training_id):
    global calibration_data_global
    user_id = session.get("user_id")

    # Check if user_id is set in the session
    if not user_id:
        logging.error("User ID is not set in session.")
        return None

    training_id_to_name = {
        '1': 'Sprinting',
        '2': 'Standing Climbing',
        '3': 'Seated Climbing',
        # Add more mappings as needed
    }

    training_name = training_id_to_name.get(str(training_id))
    if not training_name:
        print(f"No mapping found for training ID: {training_id}")
        return None

    # Attempt to retrieve calibration data from Firebase using training_name
    logging.info(f"Attempting to retrieve calibration data for user ID: {user_id}, training name: {training_name}")
    try:
        # Note the change here: using training_name in the path
        ref = db.reference(f'users/{user_id}/Calibration/Training/{training_name}/Thresholds')
        calibration_data_global = ref.get()

        # Check if the data is successfully retrieved
        if calibration_data_global:
            logging.info("Calibration data retrieved successfully.")
            logging.debug(f"Calibration data: {calibration_data_global}")
        else:
            logging.warning(f"Calibration data not found for user ID: {user_id}, training name: {training_name}")
        return calibration_data_global
    except Exception as e:
        logging.error(f"Failed to retrieve calibration data from Firebase using training name: {e}")
        return None


# Convert sensor values to categories
def convert_to_category(value, ranges):
    logging.debug(f"Converting value: {value} using ranges: {ranges}")
    try:
        if not ranges:
            raise ValueError("Empty ranges provided.")
        high_threshold = ranges.get('high', (0, float('inf')))
        medium_threshold = ranges.get('medium', (0, float('inf')))

        if value >= high_threshold[0]:
            return 'HIGH'
        elif medium_threshold[0] <= value <= medium_threshold[1]:
            return 'MEDIUM'
        elif 'low' in ranges and ranges['low'][0] <= value <= ranges['low'][1]:
            return 'LOW'
        else:
            return 'NOT ACTIVATED'
    except Exception as e:
        logging.error(f"Error in convert_to_category: {e}")
        return 'ERROR'  # Return 'ERROR' or similar to indicate a problem


def calculate_median_frequency(sensor_buffer):
    fs = 1000  # Sampling frequency, adjust as needed
    nperseg = len(sensor_buffer)  # Use the entire buffer length as one segment
    noverlap = int(nperseg * 0.25)  # Example: 25% overlap

    # Ensure noverlap is less than nperseg
    if noverlap >= nperseg:
        noverlap = nperseg - 1

    f, Pxx = welch(sensor_buffer, fs=fs, window='tukey', nperseg=nperseg, noverlap=noverlap, nfft=nperseg)
    cumulative_power = np.cumsum(Pxx)
    total_power = cumulative_power[-1]
    median_freq = f[np.searchsorted(cumulative_power, total_power / 2)]

    return median_freq


def start_bluetooth_thread():
    global bluetooth_thread, start_time
    with connection_lock:
        if bluetooth_thread is None or not bluetooth_thread.is_alive():
            start_time = time.time()  # Reset start time for state 1 timing
            bluetooth_thread = threading.Thread(target=bluetooth_communication)
            bluetooth_thread.daemon = True
            bluetooth_thread.start()

user_id = None  # Initialize user_id as a global variable
training_id = None  # Initialize training_id as a global variable


def calibration_mode():
    global collected_data, user_id, training_id, should_save_thresholds, thresholds

    logging.info(f"Starting calibration mode with user_id: {user_id}, training_id: {training_id}")

    print(f"Debug: collected_data at start of calibration_mode: {collected_data}")

    if not collected_data:  # Check if collected_data is not empty
        logging.warning("No sensor data collected for calibration.")
        print("Calibration mode exited: No sensor data collected.")
        return  # Exit the function if there is no data

    # Reset the thresholds dictionary to avoid using old data
    thresholds = {}

    # Define custom sensor names if necessary, or use SENSOR_NAMES directly
    sensor_names = SENSOR_NAMES

    # Assuming collected_data is a list of sensor values for simplicity
    # This should be adapted based on the actual structure of collected_data
    for i, sensor_data in enumerate(collected_data):
        sensor_name = sensor_names[i % len(sensor_names)]
        if not sensor_data:
            logging.warning(f"No data collected for sensor {sensor_name}.")
            continue

        # Your logic to calculate the thresholds based on sensor_data goes here
        max_value = max(sensor_data)
        activation_threshold = int(round(max_value * 0.10))
        medium_value = int(round(max_value * 0.66))  # Two-thirds of max_value

        # Determine mode within the high range
        high_values = [value for value in sensor_data if value >= medium_value]
        high_value_mode = Counter(high_values).most_common(1)[0][0] if high_values else medium_value

        # Calculate increments for the new range
        increment = int(round((high_value_mode - activation_threshold) / 3))

        # Define the ranges
        not_activated_range = (0, activation_threshold)
        low_range = (activation_threshold + 1, activation_threshold + increment)
        medium_range = (activation_threshold + increment + 1, high_value_mode - increment)
        high_range = (high_value_mode - increment + 1, high_value_mode)

        # Store the threshold ranges for this sensor
        thresholds[sensor_name] = {
            'not_activated': not_activated_range,
            'low': low_range,
            'medium': medium_range,
            'high': high_range
        }

    if thresholds:
        # Store thresholds in the session
        session['thresholds'] = thresholds
        session.modified = True  # Ensure the session is marked as modified
        logging.info(f"Calibration data calculated and stored in session: {thresholds}")
        print(f"Computed threshold values: {thresholds}")
    else:
        logging.warning("Calibration mode did not result in any thresholds data.")
        print("No thresholds computed.")


# Saving calibration data to firebase
def save_thresholds_to_firebase(user_id, training_id, thresholds):
    try:
        ref = db.reference(f'/users/{user_id}/Calibration/Training/{training_id}')
        ref.update({'Thresholds': thresholds})
        logging.info("Thresholds saved to Firebase for user_id: %s, training_id: %s", user_id, training_id)
    except Exception as e:
        logging.error("Failed to save thresholds to Firebase: %s", e)
        raise


def save_trainmode_to_firebase(user_id, training_id, sensor_name, value, category, median_freq=None):
    if not user_id or not training_id:
        print("Error: user_id or training_id is None. Cannot save data to Firebase.")
        return

    ref_path = f'/users/{user_id}/TrainingMode/{training_id}/{sensor_name}'
    ref = db.reference(ref_path)
    timestamp = int(time.time() * 1000)
    data_to_save = {'value': value, 'category': category}

    ref.child('muscle_distribution').child(str(timestamp)).set(data_to_save)
    print(f"Saved {sensor_name} data to Firebase with value {value} and category {category}.")

    if median_freq is not None:
        if median_freq is not None:
            ref.child('median_frequency').child(str(timestamp)).set({'value': median_freq})
            print(f"Saved median frequency: {median_freq} for {sensor_name} to Firebase under timestamp {timestamp}.")


sensor_buffers = {sensor_name: [] for sensor_name in SENSOR_NAMES}
@app.route('/receive-data', methods=['POST'])
def receive_data():
    global collected_data, current_state, start_time_dict, sensor_buffers, median_frequencies
    # Check current state at the very beginning
    if current_state not in [1, 3]:
        return jsonify({"error": "Not ready to receive data. Current state does not allow data reception."}), 403

    data = request.get_json()
    if 'sensor_values' not in data:
        return jsonify({"error": "Missing sensor_values"}), 400

    # Extract sensor values and convert them to integers
    sensor_values_string = data['sensor_values']
    sensor_values = [int(val) for val in sensor_values_string.split('/') if val.isdigit()]

    with connection_lock:
        # Check the state and process accordingly
        if current_state == 1:
            if 'state_1_start_time' not in start_time_dict:
                # Mark the start time for state 1
                start_time_dict['state_1_start_time'] = time.time()

            # Collect data for calibration
            collected_data.append(sensor_values)
            print(f"Collected data for calibration: {sensor_values}")

            # Check if 30 seconds have passed since entering state 1
            if time.time() - start_time_dict['state_1_start_time'] >= 30:
                # Perform calibration if 30 seconds have elapsed
                calibration_mode()
                # Transition to a different state to stop receiving data for calibration
                current_state = 2  # Assuming state 2 is a state where data reception is not allowed for calibration
                print(f"Data reception in state 1 halted as 30 seconds have elapsed.")
                return jsonify({"status": "success", "message": "Calibration complete, no longer receiving data for calibration."})

            return jsonify({"status": "success", "message": "Data received and added to calibration queue."})

        elif current_state == 3:
            categorized_data = []
            median_frequencies = {}

            for i, value in enumerate(sensor_values):
                sensor_name = SENSOR_NAMES[i % len(SENSOR_NAMES)]
                sensor_ranges = calibration_data_global.get(sensor_name, {})

                category = convert_to_category(value, sensor_ranges)
                sensor_buffers[sensor_name].append(value)

                if len(sensor_buffers[sensor_name]) >= 1000:  # Update this value as needed
                    median_freq = calculate_median_frequency(sensor_buffers[sensor_name])
                    median_frequencies[sensor_name] = median_freq
                    save_trainmode_to_firebase(user_id, training_id, sensor_name, value, category, median_freq)
                    sensor_buffers[sensor_name].clear()  # Clear the buffer after median frequency calculation
                else:
                    save_trainmode_to_firebase(user_id, training_id, sensor_name, value, category)

                categorized_data.append((sensor_name, value, category))

            return jsonify({
                "status": "success",
                "message": "Training data received and processed.",
                "median_frequencies": median_frequencies
                })

        else:
            logging.error("Data not processed due to current state restrictions.")
            return jsonify({"error": "Data not processed due to current state restrictions."}), 403

@app.route('/api/userId', methods=['POST'])
def receive_user_data():
    global user_id, training_id
    #print(f"Raw data received: {request.data}")  # Log raw data
    #data = request.get_json()
    data = request.json
    #print(f"JSON data received: {data}")  # Log JSON data
    user_id = data.get('userId')
    training_id = data.get('trainingId')

    # Store user_id in session
    session['user_id'] = user_id



    # You can now use session['user_id'] to access the user_id in other routes
    print("Received user ID:", session.get('user_id'))
    print("Received training ID:", training_id)

    # Return a response to the React Native app
    return jsonify({"status": "success", "message": "User data received successfully."})

@app.route('/confirm-save', methods=['POST'])
def confirm_save():
    global should_save_thresholds, user_id, training_id, thresholds
    try:
        # Log the current state of the variables
        logging.info(f"should_save_thresholds: {should_save_thresholds}, user_id: {user_id}, training_id: {training_id}")

        # Print the current threshold values, whether empty or not
        print(f"Current threshold values: {thresholds}")

        # Check if the save flag and user_id and training_id are set, but do not check thresholds
        if should_save_thresholds and user_id and training_id:
            logging.info("Attempting to save calibration data to Firebase, even if thresholds are empty.")
            save_thresholds_to_firebase(user_id, training_id, thresholds)
            should_save_thresholds = False  # Reset flag after saving
            return jsonify({"message": "Calibration data saved successfully."})
        else:
            return jsonify({"message": "Save operation not authorized or required data missing."}), 400
    except Exception as e:
        logging.error(f"Exception occurred while saving to Firebase: {e}")
        return jsonify({"error": "An internal server error occurred."}), 500

@app.route('/set-save-flag', methods=['POST'])
def set_save_flag():
    global should_save_thresholds
    data = request.json
    should_save_thresholds = data.get('save', False)
    return jsonify({"message": "Flag set successfully", "shouldSave": should_save_thresholds})

@app.route('/reset-timer', methods=['POST'])
def reset_timer():
    global start_time, current_state
    start_time = None  # Or reset as appropriate for your logic
    current_state = None  # Optionally reset the current state
    # Implement any additional reset logic here
    return jsonify({'message': 'Timer and state reset successfully'})


@app.route('/establish-connection', methods=['POST'])
def establish_connection():
    global data_processing_ready
    data_processing_ready = True
    # start_bluetooth_thread()
    return jsonify({'message': 'Attempting to establish connection'})

@app.route('/connection-status', methods=['GET'])
def connection_status():
    global data_processing_ready
    with connection_lock:
        return jsonify({'connected': data_processing_ready})

@app.route('/set-state-1', methods=['POST'])
def set_state_1():
    global current_state, start_time, collected_data, start_time_dict


    with connection_lock:
        current_state = 1
        start_time = time.time()
        collected_data = [[] for _ in range(6)]  # Reset collected data for new session
        start_time_dict['state_1_start_time'] = time.time()  # Reset start time for state 1
        collected_data.clear()
        #collected_data = []  # Simplify to reset to an empty list
        print("State set to 1: Reset collected_data and ready to collect new sensor data.")

    # Ensure the bluetooth_communication thread is started if not already running
    start_bluetooth_thread()
    return jsonify({'message': 'State set to 1, calibration started, ready for new sensor data'})

@app.route('/set-state-2', methods=['POST'])
def set_state_2():
    global current_state
    with connection_lock:
        current_state = 2
    return jsonify({'message': 'State set to 2'})

@app.route('/set-state-3', methods=['POST'])
def set_state_3():
    global current_state, collected_data, calibration_data_global, user_id, training_id
    data = request.json

    # Update global variables with the data received from the request
    user_id = data.get('userId')
    training_id = data.get('trainingType')

    # Log for debugging
    print(f"Received user ID: {user_id}, training ID: {training_id}")

    if not user_id or not training_id:
        return jsonify({"error": "Missing userId or trainingType"}), 400

    calibration_data_global = retrieve_calibration_data(training_id)
    if not calibration_data_global:
        return jsonify({"message": "Failed to retrieve calibration data or invalid training type."}), 400

    current_state = 3
    collected_data = []  # Reset or initialize collected data
    start_bluetooth_thread()
    return jsonify({'message': 'Training mode started, calibration data retrieved successfully.'})



@app.route('/get-state', methods=['GET'])
def get_state():
    with connection_lock:
        return jsonify({'state': current_state})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
