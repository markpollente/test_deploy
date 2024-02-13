from flask import Flask, jsonify, request, session
from flask_cors import CORS
import threading
import serial
import time
import firebase_admin
from firebase_admin import credentials, db
from flask_session import Session
from collections import Counter
import logging

#import random  # Only if you need to simulate data collection
app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = '31a6d43a34178b9d483370a095e426d2'  # Replace with a secure secret key
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


# Define your training goals for each training type
training_goals = {
    'seated_climbing': ['quads_r', 'quads_l', 'hams_r', 'hams_l', 'glutes_r', 'glutes_l'],
    'standing_climbing': ['quads_r', 'quads_l', 'glutes_r', 'glutes_l'],
    'sprinting': ['quads_r', 'quads_l']
}

# Global variables
current_state = None
bluetooth_thread = None
start_time = None
bluetooth_connected = False
bluetooth_serial = None
connection_lock = threading.Lock()
collected_data = []  # To store data collected during calibration
calibration_data_global={}
should_save_thresholds = False # Flag to control saving of thresholds
# Variable to indicate readiness to process data
data_processing_ready = False


'''def bluetooth_communication():
    global collected_data
    try:
        while True:
            time.sleep(1)
            with connection_lock:
                if not collected_data:
                    continue  # Skip this iteration if there's no data to process

                # Copy collected_data for processing and clear the original list to avoid reprocessing
                data_to_process = collected_data[:]
                collected_data = []

            # Process each sublist of sensor values outside the lock to minimize lock contention
            for sensor_values_set in data_to_process:
                if sensor_values_set:
                    print(f"Processing: {sensor_values_set}")  # Placeholder for actual processing logic
                else:
                    print("Encountered an empty set, which should not happen with correct data flow.")

    except Exception as e:
        print(f"Unexpected error in bluetooth_communication: {e}")'''



def bluetooth_communication():
    global start_time, current_state, bluetooth_connected, bluetooth_serial, collected_data
    try:

        while True:
            if current_state == 1 or current_state == 3:
                # For both states 1 and 3, read and store data from the Bluetooth device
                # Check if there's new data to process
                if collected_data:
                    # Assuming collected_data is a list of lists, each sublist being a set of sensor values
                    for sensor_values_set in collected_data:
                        read_and_store_data(sensor_values_set)

                    if current_state == 1 and (time.time() - start_time) > 10:

                        # Specific processing after 5 seconds have passed in state 1
                        print("5 seconds have passed in state 1. Processing data.")
                        calibration_mode()  # Process and save the data collected during state 1
                        current_state = None  # Reset state after processing
                        collected_data = []  # Clear collected data for the next session
                        break   # Exit the while loop

    except serial.SerialException as e:
        print(f"Serial exception: {e}")
        bluetooth_connected = False
        bluetooth_serial = None  # Ensure serial connection is reset
    except Exception as e:
        print(f"Unexpected error: {e}")
        bluetooth_connected = False
        bluetooth_serial = None  # Ensure serial connection is reset
    finally:
        pass


def read_and_store_data(sensor_values=None):
    global collected_data, calibration_data_global, current_state
    if sensor_values is None:
        sensor_values = []
    sensor_names = ['R_quads', 'R_hams', 'R_glutes', 'L_quads', 'L_hams', 'L_glutes']
    num_sensors = len(sensor_names)

    # Log the received sensor values for debugging
    print(f"Received sensor values: {sensor_values}")

    if len(sensor_values) % num_sensors != 0:
        print(f"Warning: Received {len(sensor_values)} sensor values, which does not evenly divide by {num_sensors} sensors.")
    else:
        # Process sensor values
        for i, value in enumerate(sensor_values):
            if i >= num_sensors:
                print("Error: More sensor values received than expected based on sensor_names length.")
                break
            sensor_name = sensor_names[i % num_sensors]
            sensor_ranges = calibration_data_global.get(sensor_name, {})
            category = convert_to_category(value, sensor_ranges) if current_state == 3 else 'N/A'
            # Only call save_trainmode_to_firebase in state 3 and if all required arguments are available
            if current_state == 3 and all([user_id, training_id, sensor_name, value, category != 'N/A']):
                save_trainmode_to_firebase(user_id, training_id, sensor_name, value, category)
            if current_state == 1:
                collected_data.append(value)  # This assumes collected_data should capture all sensor values in calibration mode


# Retrieve calibration data from Firebase
def retrieve_calibration_data(training_id):
    ref = db.reference(f'users/{session.get("user_id")}/Calibration/Training/{training_id}/Thresholds')
    calibration_data_global = ref.get()
    if calibration_data_global:
        print("Calibration data retrieved successfully.")
        print("Calibration data:", calibration_data_global)
    else:
        print("Failed to retrieve calibration data.")
    return calibration_data_global

# Convert sensor values to categories
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
        high_values = [value for data_list in collected_data for value in data_list if high_range[0] <= value <= high_range[1]]
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

# Saving calibration data to firebase
def save_thresholds_to_firebase(user_id, training_id, thresholds):
    ref = db.reference(f'/users/{user_id}/Calibration/Training/{training_id}')
    ref.update({
        'Thresholds': thresholds
    })
    print("Thresholds saved to Firebase.")

#Saving each data point during training
def save_trainmode_to_firebase(session, user_id, training_id,sensor_name, value, category):
    # Define the Firebase reference path based on category
    ref_path = f'/users/{user_id}/TrainingMode/{training_id}/{sensor_name}/{category}'
    ref = db.reference(ref_path)

    # Save data point with a timestamp as the key or any unique identifier
    timestamp = int(time.time() * 1000)  # Using current time in milliseconds as a unique identifier
    ref.child(str(timestamp)).set({
        'value': value,
    })
    print(f"Saved {sensor_name} data to Firebase under {category} category.")

def fetch_high_counts_for_muscle_group(training_id, sensor_name, category="HIGH"):
    # Construct the path to the muscle group data
    path = f'/users/{user_id}/Training/{training_id}/{sensor_name}/HIGH'

    # Get a reference to the Firebase Realtime Database
    ref = db.reference(path)

    # Fetch the data
    data = ref.get()
    if not data:
        print(f"No data found for {sensor_name}")
    return len(data)  # Return the count of 'HIGH' data points


def calculate_percentage_high(high_count, total_count):
    return (high_count / total_count * 100) if total_count > 0 else 0
# Function to calculate the percentage of 'HIGH' activation for each muscle group
def calculate_percentage_high(training_ID, sensor_name):
    percentages = {}
    for sensor_name in sensor_name:
        high_count = fetch_high_counts(training_id, sensor_name)
        # Assuming you also have a function to fetch the total counts, not shown here
        total_count = fetch_total_counts(training_id, sensor_name)
        percentage = (high_count / total_count * 100) if total_count > 0 else 0
        percentages[sensor_name] = percentage
    return percentages

def fetch_total_counts(training_id, sensor_name):
    # Construct the path to the muscle group data
    path = f'/users/{user_id}/Training/{training_id}/{sensor_name}'

    # Get a reference to the Firebase Realtime Database
    ref = db.reference(path)

    # Fetch the data
    data = ref.get()
    if not data:
        return 0  # No data for the muscle group

    # Count the total number of entries
    total_count = sum(len(category_data) for category_data in data.values() if isinstance(category_data, dict))

    return total_count


# Main function that ties everything together
def main_function():
    # Define the training type - this could come from user input, a database query, etc.
    selected_training_type = 'standing_climbing'  # Example training type

    # Check that the selected training type is in the training goals
    if selected_training_type not in training_goals:
        raise ValueError(f"Training type '{selected_training_type}' is not recognized.")

    # Calculate the percentage of 'HIGH' activation for each muscle group
    sensor_name = training_goals[selected_training_type]
    percentages = calculate_percentage_high(selected_training_type, muscle_groups)

    # Display the results
    for muscle_group, percentage in percentages.items():
        print(f"{sensor_name}: {percentage:.2f}% HIGH")
def save_post_analysis_results(session_id, analysis_data):
    # Save the post-analysis data to Firebase under the session ID
    results_ref = db.reference(f'Training/{session_id}/post_analysis_results')
    results_ref.set(analysis_data)


@app.route('/receive-data', methods=['POST'])
def receive_data():
    global collected_data

    if current_state not in [1, 3]:
        return jsonify({"error": "Not ready to receive data. Current state does not allow data reception."}), 403

    data = request.get_json()
    #print(f"Request data: {data}")  # Log all incoming data for debugging

    if 'sensor_values' in data:
        sensor_values_string = data['sensor_values']
        #print(f"Received sensor values string: {sensor_values_string}")

        # Convert the string of sensor values into a list of integers
        sensor_values = [int(val) for val in sensor_values_string.split('/') if val.isdigit()]
        #print(f"Processed sensor values: {sensor_values}")

        with connection_lock:
            # Append the list of sensor values as a new sublist in collected_data
            collected_data.append(sensor_values)
            #print(f"Updated collected_data with received sensor values: {collected_data}")
            read_and_store_data(sensor_values)
        return jsonify({"status": "success", "message": "Data received and processed."})
    else:
        return jsonify({"error": "Missing sensor_values"}), 400

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
@app.route('/confirm-save', methods=['POST'])
def confirm_save():
    global should_save_thresholds, user_id, training_id, thresholds
    # Logging the values

    if should_save_thresholds and user_id:

        print("Saving calibration data based on user confirmation.")
        save_thresholds_to_firebase(user_id, training_id, thresholds)
        should_save_thresholds = False  # Reset flag after saving
        return jsonify({"message": "Calibration data saved successfully."})
    else:
        return jsonify({"message": "Save operation not authorized or no data to save."}), 400

@app.route('/set-save-flag', methods=['POST'])
def set_save_flag():
    global should_save_thresholds
    data = request.json
    should_save_thresholds = data.get('save', False)
    return jsonify({"message": "Flag set successfully", "shouldSave": should_save_thresholds})

@app.route('/start-calibration', methods=['POST'])
def start_calibration():
    global current_state, collected_data
    current_state = 1
    collected_data = []  # Reset collected data
    threading.Thread(target=collect_data).start()
    return jsonify({"message": "Calibration started."})

@app.route('/stop-and-process', methods=['POST'])
def stop_and_process():
        # Assume the POST request includes the session ID for identifying the training session
        data = request.get_json()
        session_id = data.get('session_id')
        if not session_id:
            return jsonify({"message": "Session ID is required."}), 400

        # Perform post-analysis calculations
        selected_training, sensor_data = fetch_session_data(session_id)
        sensor_name = training_goals[selected_training]
        percentages = calculate_percentage_high(selected_training, sensor_name)

        # Optionally, save the post-analysis results to Firebase
        save_post_analysis_results(session_id, percentages)

        # Return the analysis results
        return jsonify({"message": "Post-analysis completed.", "percentages": percentages})




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
    global current_state, start_time, collected_data


    with connection_lock:
        current_state = 1
        start_time = time.time()
        collected_data = [[] for _ in range(6)]  # Reset collected data for new session
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
    global current_state, collected_data, calibration_data_global
    data = request.json
    training_id = data.get('trainingType')

    # Retrieve calibration data for the selected training type
    calibration_data_global = retrieve_calibration_data(training_id)

    if not calibration_data_global:
        return jsonify({"message": "Failed to retrieve calibration data or invalid training type."}), 400

    current_state = 3  # Assuming state 4 is for training mode
    collected_data = [[] for _ in range(6)]  # Reset collected data for new session
    start_bluetooth_thread()  # Ensure Bluetooth thread is running
    return jsonify({'message': 'Training mode started, calibration data retrieved'})

@app.route('/get-state', methods=['GET'])
def get_state():
    with connection_lock:
        return jsonify({'state': current_state})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
