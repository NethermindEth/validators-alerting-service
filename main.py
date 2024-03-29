
import requests
import sqlite3
import os
import logging
from sseclient import SSEClient
import asyncio
import json

# Environment variables
NETWORK=os.getenv('NETWORK') # Network name
DATABASE=os.getenv('DATABASE') # Local or remote DB name/url
BEACON_URL=os.getenv('BEACON_URL') # Comma separated list of beacon nodes
MISSED_ATTESTATIONS_ALLOWANCE=os.getenv('MISSED_ATTESTATIONS_ALLOWANCE') # Maximum amount of missed attestation before triggering an alert
TABLE_NAME=os.getenv('TABLE_NAME') # Name of the table in database
OPSGENIE_KEY=os.getenv('OPSGENIE_KEY') # API Key for OpsGenie alerting service
OPSGENIE_TEAM_ID=os.getenv('OPSGENIE_TEAM_ID') # Id of the routing team
SPREADSHEET=os.getenv('SPREADSHEET') # Link to validators dashboard for reference
VALIDATORS=os.getenv('VALIDATORS') # String of comma separated validator indexes

logging.basicConfig(format='%(asctime)s | %(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# logger.addHandler(watchtower.CloudWatchLogHandler(log_group_name=f"ValidatorsAlertingService_{NETWORK}"))

# Connects to sqlite database
try:
    con = sqlite3.connect(DATABASE)
    cur = con.cursor()
    logger.info(f"The connection with {DATABASE} has been established.")
except sqlite3.Error as err:
    logger.error(err)

# Creating database table if it does not exist
# Creating unique indexes on validator's index value, also if not exists already
async def create_table(table):
    sql = f'create table if not exists {table} (ind integer, balance integer, missed_attestations_current integer default 0, missed_attestations_total integer default 0)'
    sql_unique = f'create unique index if not exists validators_index on {table} ( ind ) ;'
    try:
        cur.execute(sql)
        cur.execute(sql_unique)
        logger.info(f"The table {table} has been created or skipped.")
    except sqlite3.Error as err:
        logger.error(err)

# Gets the validators balances from /eth/v1/beacon/states/head/validator_balances url
# Inserts that data to the previously created table
# Tracks the balance of each validators and counts the missed attestations (when balance decreases)
async def get_validator_balances(url, validators, table_name, epoch, checkpoint_topic, total_balance):

    endpoint = f'{url}/eth/v1/beacon/states/head/validator_balances?id={validators}'
    
    try:
        r = requests.get(endpoint, timeout=5.0)
        r.raise_for_status()
        data = r.json()['data']
        
        committee_validators = await get_committee(url)

        for validator in data:
            try:
                cur.execute(f'INSERT OR IGNORE INTO {table_name} (ind, balance, missed_attestations_current, missed_attestations_total) VALUES (?,?,?,?)',(validator['index'], validator['balance'],0,0))
            except sqlite3.Error as err:
                logger.error(err)
            for item in cur.execute(f'SELECT * FROM {table_name} WHERE ind = {validator["index"]}'):
                balance = item[1]
                missed_attestations_current = item[2]
                missed_attestations_total = item[3]
                total_balance += balance
                if balance > int(validator['balance']):
                    logger.warning(f'Attestation has been missed by {validator["index"]}, count: {missed_attestations_current +1}')
                    try:
                        if validator['index'] in committee_validators and NETWORK != 'gnosis':
                            logging.error(f'Validator {validator["index"]} is in the committee and is misbehaving.')
                            cur.execute(f'REPLACE INTO {table_name} (ind, balance, missed_attestations_current, missed_attestations_total) VALUES (?,?,?,?)',(validator['index'], validator['balance'], missed_attestations_current +1, missed_attestations_total +1))
                        else:
                            cur.execute(f'REPLACE INTO {table_name} (ind, balance, missed_attestations_current, missed_attestations_total) VALUES (?,?,?,?)',(validator['index'], validator['balance'], missed_attestations_current +1, missed_attestations_total +1))
                    except sqlite3.Error as err:
                        logger.error(err)
                else:
                    try:
                        cur.execute(f'INSERT OR REPLACE INTO {table_name} (ind, balance, missed_attestations_current, missed_attestations_total) VALUES (?,?,?,?)',(validator['index'], validator['balance'], 0, missed_attestations_total))
                    except sqlite3.Error as err:
                        logger.error(err)
        
        if NETWORK == 'gnosis':
            total_balance_formatted = (total_balance/10**9)/32
            total_earned = ((total_balance/10**9) - len(VALIDATORS.split(','))*32)/32
        else:
            total_balance_formatted = (total_balance/10**9)
            total_earned = (total_balance_formatted - len(VALIDATORS.split(','))*32)

        logger.info(f"Total balance: {total_balance_formatted} {'GNO' if NETWORK == 'gnosis' else 'ETH'}")
        logger.info(f"Total earned: {total_earned} {'GNO' if NETWORK == 'gnosis' else 'ETH'}")           
        logger.info(f'Epoch: {epoch}, inserting data to the {table_name} table.')

    except requests.exceptions.HTTPError:
        logger.error(f'Connection to {url} timed out.')
        logger.info("Closing connection with SSE stream.")
        checkpoint_topic.close()
            
    except requests.exceptions.RequestException as err:
        logger.error(f'Error: {err}')
        logger.info("Closing connection with SSE stream.")
        checkpoint_topic.close()

# Gets the list of validators that have missed at least 1 attestation in total
async def get_validators_with_missed_attestations(table):
    with con:
        try:
            cur.execute(f"SELECT ind, missed_attestations_current, missed_attestations_total FROM {table} WHERE missed_attestations_total > 0")
        except sqlite3.Error as err:
            logger.error(err)
        logger.info(f"Validators that missed attestations in total: {cur.fetchall()}")

# Determines whether validator is active or not by checking its current missed attestation count
# Upon MISSED_ATTESTATIONS_ALLOWANCE trigger and alert indicating that the node is most likely offline
async def alert_on_validator_inactivity(table):
    with con:
        try:
            inactive_validators = cur.execute(f"SELECT ind, missed_attestations_current FROM {table} WHERE missed_attestations_current >= {MISSED_ATTESTATIONS_ALLOWANCE}").fetchall()
            if len(inactive_validators) > 0:
                await send_alert(inactive_validators)
        except sqlite3.Error as err:
            logger.error(err)
            
async def get_committee(url):
    endpoint = f'{url}/eth/v1/beacon/states/finalized/sync_committees'
    try:
        r = requests.get(endpoint, timeout=5.0)
        r.raise_for_status()
        return r.json()['data']['validators']
    except requests.exceptions.HTTPError as err:
        logger.error(f'Error: {err}')
    except requests.exceptions.RequestException as err:
        logger.error(f'Error: {err}')


# Sends POST message to OpsGenie service
async def send_alert(inactive_validators):
    validators_indexes = ''
    endpoint = "https://api.eu.opsgenie.com/v1/incidents/create"
    for (index, missed_attestations) in inactive_validators:
        validators_indexes += f"{index},"
    payload = json.dumps({
      "message": f"{NETWORK.capitalize()} Validators Down",
      "description": f"{len(inactive_validators)} {NETWORK} validators inactive: {validators_indexes[:-1]}\nLook up: {SPREADSHEET}",
      "responders": [
        {
          "id": OPSGENIE_TEAM_ID,
          "type": "team"
        }
      ],
      "tags": [
        "Outage",
        "Critical"
      ],
      "priority": "P1"
    })
    headers = {
      'Content-Type': 'application/json',
      'Authorization': f'GenieKey {OPSGENIE_KEY}'
    }
    try:
        r = requests.post(endpoint, headers=headers, data=payload.encode('utf8'), timeout=5.0)
        r.raise_for_status()
        logger.info(f"Sending an alert for validators: {validators_indexes[:-1]}")
    except requests.exceptions.HTTPError as err:
        logger.error(f'Error: {err}')
    except requests.exceptions.RequestException as err:
        logger.error(f'Error: {err}')

# Reads number of validators to be monitored
# Executes create_table function
# Checks the endpoints availability with check_beacon_url function
# Runs SSE client to listen to finalized_checkpoint topic on beacon node
# Executes get_validator_balances function on each new event
async def main():
    validators = VALIDATORS.split(',')
    urls = BEACON_URL.split(',')
    logger.info(f"{NETWORK} validators monitored: {len(validators)}")
    total_balance = 0
    
    await create_table(TABLE_NAME)

    while True:
        for index, url in enumerate(urls):
            try:
                fallback = index + 1
                stream = f"{url}/eth/v1/events?topics=finalized_checkpoint"
                checkpoint_topic = SSEClient(stream)

                logger.info(f'Connected to: {url}')
                for event in checkpoint_topic:
                    if len(event.data) == 0: break
                    logger.info("Received finalized checkpoint from events stream.")
                    logger.info(event.data)
                    epoch = json.loads(event.data)["epoch"]
                    await get_validator_balances(url, VALIDATORS, TABLE_NAME, epoch, checkpoint_topic, total_balance)
                    await alert_on_validator_inactivity(TABLE_NAME)
                break
            except Exception as err:
                try:
                    logger.error(f'Failed connecting to {url}. Falling back to {urls[fallback]}. Error: {err}')
                except IndexError:
                    logger.error(f'All endpoints are down: {urls}')
                continue

asyncio.run(main())