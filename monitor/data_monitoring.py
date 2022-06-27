import json
import pkg_resources
import os
import webbrowser


from monitor.api.models.models import ModelsAPI
from monitor.api.sidebar.sidebar import SidebarAPI
from monitor.api.tests.tests import TestsAPI
from monitor.test_result import TestResult
from clients.dbt.dbt_runner import DbtRunner
from config.config import Config
from clients.slack.slack_client import SlackClient
from clients.slack.schema import SlackMessageSchema
from utils.log import get_logger
from utils.time import get_now_utc_str
from alive_progress import alive_it
from typing import Dict, List, Optional, Tuple

logger = get_logger(__name__)
FILE_DIR = os.path.dirname(__file__)

YAML_FILE_EXTENSION = ".yml"
SQL_FILE_EXTENSION = ".sql"


class DataMonitoring(object):
    DBT_PACKAGE_NAME = 'elementary'
    DBT_PROJECT_PATH = os.path.join(FILE_DIR, 'dbt_project')
    DBT_PROJECT_MODELS_PATH = os.path.join(FILE_DIR, 'dbt_project', 'models')
    # Compatibility for previous dbt versions
    DBT_PROJECT_MODULES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_modules', DBT_PACKAGE_NAME)
    DBT_PROJECT_PACKAGES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_packages', DBT_PACKAGE_NAME)

    def __init__(
        self,
        config: Config,
        force_update_dbt_package: bool = False,
        slack_webhook: Optional[str] = None, 
        slack_token: Optional[str] = None,
        slack_channel_name: Optional[str] = None
    ) -> None:
        self.config = config
        self.dbt_runner = DbtRunner(self.DBT_PROJECT_PATH, self.config.profiles_dir, self.config.profile_target)
        self.execution_properties = {}
        self.slack_webhook = slack_webhook or self.config.slack_notification_webhook
        self.slack_token = slack_token or self.config.slack_token
        self.slack_channel_name = slack_channel_name or self.config.slack_notification_channel_name
        # slack client is optional
        self.slack_client = SlackClient.create_slack_client(token=self.slack_token, webhook=self.slack_webhook) if (self.slack_token or self.slack_webhook) else None
        self._download_dbt_package_if_needed(force_update_dbt_package)
        self.success = True

    def _dbt_package_exists(self) -> bool:
        return os.path.exists(self.DBT_PROJECT_PACKAGES_PATH) or os.path.exists(self.DBT_PROJECT_MODULES_PATH)

    @staticmethod
    def _split_list_to_chunks(items: list, chunk_size: int = 50) -> List[List]:
        chunk_list = []
        for i in range(0, len(items), chunk_size):
            chunk_list.append(items[i: i + chunk_size])
        return chunk_list

    def _update_sent_alerts(self, alert_ids) -> None:
        alert_ids_chunks = self._split_list_to_chunks(alert_ids)
        for alert_ids_chunk in alert_ids_chunks:
            self.dbt_runner.run_operation(macro_name='update_sent_alerts',
                                          macro_args={'alert_ids': alert_ids_chunk},
                                          json_logs=False)

    def _query_alerts(self, days_back: int) -> list:
        results = self.dbt_runner.run_operation(macro_name='get_new_alerts', macro_args={'days_back': days_back})
        test_result_alerts = []
        if results:
            test_result_alert_dicts = json.loads(results[0])
            self.execution_properties['alert_rows'] = len(test_result_alert_dicts)
            for test_result_alert_dict in test_result_alert_dicts:
                test_result_object = TestResult.create_test_result_from_dict(
                    test_result_dict=test_result_alert_dict,
                )
                if test_result_object:
                    test_result_alerts.append(test_result_object)
                else:
                    self.success = False

        return test_result_alerts

    def _send_to_slack(self, test_result_alerts: List[TestResult]) -> None:
        sent_alerts = []
        alerts_with_progress_bar = alive_it(test_result_alerts, title="Sending alerts")
        for alert in alerts_with_progress_bar:
            alert_slack_message: SlackMessageSchema = alert.generate_slack_message(is_slack_workflow=self.config.is_slack_workflow)
            sent_successfully = self.slack_client.send_message(
                channel_name=self.slack_channel_name,
                message=alert_slack_message
            )
            if sent_successfully:
                sent_alerts.append(alert.id)
            else:
                logger.error(f"Could not sent the alert - {alert.id}. Full alert: {json.dumps(dict(alert_slack_message))}")
                self.success = False
        
        sent_alert_count = len(sent_alerts)
        self.execution_properties['sent_alert_count'] = sent_alert_count
        if sent_alert_count > 0:
            self._update_sent_alerts(sent_alerts)

    def _download_dbt_package_if_needed(self, force_update_dbt_packages: bool):
        internal_dbt_package_exists = self._dbt_package_exists()
        self.execution_properties['dbt_package_exists'] = internal_dbt_package_exists
        self.execution_properties['force_update_dbt_packages'] = force_update_dbt_packages
        if not internal_dbt_package_exists or force_update_dbt_packages:
            logger.info("Downloading edr internal dbt package")
            package_downloaded = self.dbt_runner.deps()
            self.execution_properties['package_downloaded'] = package_downloaded
            if not package_downloaded:
                logger.info('Could not download internal dbt package')
                self.success = False
                return

    def _send_alerts(self, days_back: int):
        alerts = self._query_alerts(days_back)
        alert_count = len(alerts)
        self.execution_properties['alert_count'] = alert_count
        if alert_count > 0:
            self._send_to_slack(alerts)

    def run(self, days_back: int, dbt_full_refresh: bool = False) -> bool:

        logger.info("Running internal dbt run to aggregate alerts")
        success = self.dbt_runner.run(models='alerts', full_refresh=dbt_full_refresh)
        self.execution_properties['alerts_run_success'] = success
        if not success:
            logger.info('Could not aggregate alerts successfully')
            self.success = False
            self.execution_properties['success'] = self.success
            return self.success

        self._send_alerts(days_back)
        self.execution_properties['run_end'] = True
        self.execution_properties['success'] = self.success
        return self.success

    def generate_report(self) -> Tuple[bool, str]:
        now_utc = get_now_utc_str()

        elementary_output = {}
        elementary_output['creation_time'] = now_utc
        test_results, test_result_totals = self._get_test_results_and_totals()
        models, dbt_sidebar = self._get_dbt_models_and_sidebar()
        elementary_output['models'] = models
        elementary_output['dbt_sidebar'] = dbt_sidebar
        elementary_output['test_results'] = test_results
        elementary_output['totals'] = test_result_totals

        html_index_path = pkg_resources.resource_filename(__name__, "index.html")
        with open(html_index_path, 'r') as index_html_file:
            html_code = index_html_file.read()
            elementary_output_str = json.dumps(elementary_output)
            elementary_output_html = f"""
                    {html_code}
                    <script>
                        var elementaryData = {elementary_output_str}
                    </script>
                """
            elementary_html_file_name = f"elementary - {now_utc} utc.html".replace(" ", "_").replace(":", "-")
            elementary_html_path = os.path.join(self.config.target_dir, elementary_html_file_name)
            with open(elementary_html_path, 'w') as elementary_output_html_file:
                elementary_output_html_file.write(elementary_output_html)
            with open(os.path.join(self.config.target_dir, 'elementary_output.json'), 'w') as \
                    elementary_output_json_file:
                elementary_output_json_file.write(elementary_output_str)

            elementary_html_file_path = 'file://' + elementary_html_path
            webbrowser.open_new_tab(elementary_html_file_path)
            self.execution_properties['report_end'] = True
            self.execution_properties['success'] = self.success
            return self.success, elementary_html_path
    
    def send_report(self, elementary_html_path: str) -> bool:
        if os.path.exists(elementary_html_path):
            file_uploaded_succesfully = self.slack_client.upload_file(
                channel_name=self.slack_channel_name,
                file_path=elementary_html_path,
                message=SlackMessageSchema(text="Elementary monitoring report")
            )
            if not file_uploaded_succesfully:
                self.success = False
        else:
            logger.error('Could not send Elementary monitoring report because it does not exist')
            self.success = False
        return self.success

    def _get_test_results_and_totals(self):
        tests_api = TestsAPI(dbt_runner=self.dbt_runner)
        try:
            tests = tests_api.get_tests_metadata()
            totals = tests_api.get_total_tests_results(tests["raw_tests"])
            self.execution_properties['test_results'] = tests["count"]
            return tests["tests"], totals
        except Exception as e:
            logger.error(f"Could not get test results and totals - Error: {e}")
            self.success = False
            return dict(), dict()

    def _get_dbt_models_and_sidebar(self) -> Tuple[Dict, Dict]:
        models_api = ModelsAPI(dbt_runner=self.dbt_runner)
        sidebar_api = SidebarAPI(dbt_runner=self.dbt_runner)

        models = models_api.get_models()
        sources = models_api.get_sources()

        models_and_sources = dict(**models, **sources)
        serializeable_models = dict()
        for key in models_and_sources.keys():
            serializeable_models[key] = dict(models_and_sources[key])

        dbt_sidebar = sidebar_api.get_sidebar(models=models, sources=sources)
        
        return serializeable_models, dbt_sidebar

    def properties(self):
        data_monitoring_properties = {'data_monitoring_properties': self.execution_properties}
        return data_monitoring_properties
