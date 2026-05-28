module.exports = {
  apps: [
    {
      name: "matrix-dispatcher-forge",
      script: "/home/ted/repos/personal/matrix-dispatcher/start-forge.sh",
      interpreter: "bash",
      cwd: "/home/ted/repos/personal/matrix-dispatcher",
      restart_delay: 5000,
      max_restarts: 10,
      out_file: "/home/ted/.pm2/logs/matrix-dispatcher-forge-out.log",
      error_file: "/home/ted/.pm2/logs/matrix-dispatcher-forge-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
