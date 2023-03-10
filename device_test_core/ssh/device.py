"""SSH Device Adapter"""
import glob
import logging
import os
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Tuple, Dict, Optional
from datetime import datetime, timezone, timedelta
from device_test_core.adapter import DeviceAdapter
from device_test_core.file_utils import make_tarfile


try:
    import paramiko
except ImportError:
    raise ImportError(
        "Importing Paramiko library failed. " "Make sure you have Paramiko installed."
    )

from paramiko.client import SSHClient

try:
    import scp
    from scp import SCPClient
except ImportError:
    raise ImportError(
        "Importing SCP library failed. " "Make sure you have SCP installed."
    )


log = logging.getLogger(__name__)


class SSHDeviceAdapter(DeviceAdapter):
    """SSH connected Device"""

    # pylint: disable=too-many-public-methods

    def __init__(
        self,
        name: str,
        device_id: str = None,
        env: Dict[str, str] = None,
        should_cleanup: bool = None,
        config: Dict[str, Any] = None,
    ):
        super().__init__(name, device_id, should_cleanup=should_cleanup, config=config)
        self._env = env or {}
        self._client = SSHClient()
        self._client.load_system_host_keys()
        self._connect()

    @property
    def is_existing_device(self) -> bool:
        """Is existing device

        Returns:
            bool: If this device is an existing device
        """
        return self._is_existing_device

    @is_existing_device.setter
    def is_existing_device(self, is_existing_device: bool):
        """Set the is_existing_device

        Args:
            is_existing_device (bool): If this device is an existing device
        """
        self._is_existing_device = is_existing_device

    @property
    def test_start_time(self) -> datetime:
        """Test start time (in utc)

        Returns:
            datetime: Start time of the test
        """
        return self._test_start_time

    @test_start_time.setter
    def test_start_time(self, now: datetime):
        """Set the test start time

        Args:
            now (datetime): Datetime when the test started
        """
        self._test_start_time = now

    @property
    def start_time(self) -> datetime:
        """Get the start time of the device

        Returns:
            datetime: Device start time. None if the device does not exist
        """
        output = self.assert_command("awk '{print $1}' /proc/uptime")
        uptime = int(float(output.decode("utf-8").strip()))
        return datetime.now(timezone.utc) - timedelta(seconds=uptime)

    def get_uptime(self) -> float:
        """Get device uptime in seconds

        A zero is returned if the device does not exist

        Returns:
            int: Uptime in seconds
        """
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    def get_device_stats(self) -> Any:
        """Get device statistics (i.e. cpu, network traffic etc.)

        Returns:
            Optional[Any]: Device stats object
        """
        raise NotImplementedError("Device statistics is not supported when using SSH")

    def _connect(self):
        hostname = self._config.get("hostname")
        username = self._config.get("username", None)
        password = self._config.get("password", None)

        assert hostname, "Missing hostname from adapter configuration"
        self._client.connect(hostname, username=username, password=password)

    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> Tuple[int, Any]:
        """Execute a command

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Device not found error

        Returns:
            Tuple[int, Any]: Command output (exit_code, output)
        """
        run_cmd = []

        use_sudo = self.use_sudo()
        if use_sudo:
            run_cmd.extend(["sudo", "-E"])

        if self._env:
            log.info("Setting environment variables")
            envs = ["env"] + [f"{key}={value}" for key, value in self._env.items()]
            run_cmd.extend(envs)

        if shell:
            run_cmd.extend(["/bin/bash", "-c"])

        if isinstance(cmd, (list, tuple)):
            run_cmd.extend(cmd)
        else:
            run_cmd.append(cmd)

        tran = self._client.get_transport()
        timeout = kwargs.pop("timeout", 120)
        chan = tran.open_session(timeout=timeout)

        chan.get_pty()
        f = chan.makefile()
        chan.exec_command(shlex.join(run_cmd))
        output = f.read()

        # Note: Replace the \r which are added to due the simulated terminal
        # https://stackoverflow.com/questions/35887380/why-does-paramiko-returns-r-n-as-newline-instead-of-n
        output = output.replace(b"\r\n", b"\n")
        # Check exist status after calling read, otherwise it hangs
        # https://github.com/paramiko/paramiko/issues/448
        exit_code = chan.recv_exit_status()
        f.close()

        # Option 2: Use more simple approach, but the stdout and stderr is separated
        # stdin, stdout, stderr = self._client.exec_command(shlex.join(cmd))
        # exit_code = stdout.channel.recv_exit_status()
        # output = "\n".join(stdout.readlines())
        if log_output:
            logging.info(
                "cmd: %s, exit code: %d, stdout:\n%s",
                cmd,
                exit_code,
                output.decode("utf-8"),
            )
        return exit_code, output

    @property
    def name(self) -> str:
        """Get the name of the device

        Returns:
            str: Device name
        """
        return self._name

    def restart(self):
        """Restart device"""
        logging.info("Restarting %s", self.name)
        self.assert_command("shutdown -r now")
        time.sleep(120)  # Wait for system to go down (incase it gets this far)
        raise Exception("System did not restart")

    def get_ipaddress(self) -> Optional[str]:
        """Get IP address of the device"""
        return self._config.get("hostname")

    def disconnect_network(self):
        """Disconnect device from the network"""
        raise NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def connect_network(self):
        """Connect device to the network"""
        raise NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def get_id(self) -> str:
        """Get the device id

        Raises:
            Exception: Device id not found

        Returns:
            str: Device id
        """
        return self._device_id

    def use_sudo(self) -> bool:
        return True

    def copy_to(self, src: str, dst: str):
        """Copy file to the device

        Args:
            src (str): Source file (on host)
            dst (str): Destination (on device)
        """
        try:
            total_files = 0
            archive_path = ""

            # build archive
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".tar", delete=False
            ) as file:
                total_files = make_tarfile(file, [src])
                archive_path = file.name

            if total_files > 1 or dst.endswith("/") or dst in [".", ".."]:
                parent_dir = dst.rstrip("/") + "/"
            else:
                parent_dir = os.path.dirname(dst)

            # copy archive to device
            tmp_dst = f"/tmp/{Path(archive_path).name}"
            with SCPClient(self._client.get_transport()) as scp_client:
                scp_client.put(archive_path, recursive=True, remote_path=tmp_dst)

            self.assert_command(f"mkdir -p '{parent_dir}'")
            self.assert_command(
                f"tar xf '{tmp_dst}' -C '{parent_dir}' && rm -f '{tmp_dst}'"
            )

        finally:
            if archive_path and os.path.exists(archive_path):
                os.unlink(archive_path)

    def cleanup(self, force: bool = False):
        """Cleanup the device. This will be called when the define is no longer needed"""
        if not force and not self.should_cleanup:
            log.info("Skipping cleanup due to should_cleanup not being set")
            return

        if self._client:
            self._client.close()
