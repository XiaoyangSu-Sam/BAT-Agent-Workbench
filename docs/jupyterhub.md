# Run a conversation on JupyterHub

Keep editing files in the BAT workbench and run Python in your own existing JupyterHub environment. Select a kernel for each conversation. Its variables and imports are available to later cells until the kernel stops.

## Connect

1. Sign in to your JupyterLab instance and start your default personal server. Check that the Python kernel for your desired environment is listed. Upload input data and supporting modules to your chosen remote folder in JupyterLab.
2. In BAT, open a conversation. Choose **Run on Local** near the composer. Enter the HTTPS **Hub URL**, including any deployment prefix, such as `https://hub.example.org/team/`. You can also paste the Hub's login or home URL, or the path to a personal JupyterLab page. Omit query parameters and fragments.
3. Choose **Username and password** for the Hub's standard login form, or **API token** for a personal Hub token permitted by your administrator. For interactive SSO or MFA, sign in through your usual browser and use the token option if your Hub allows it. Enter credentials in the connection dialog.
4. Choose **Discover kernels**, select a Python kernel, and enter an existing remote working directory relative to the JupyterLab root. An empty directory field selects the root. Choose **Connect**. BAT starts a new kernel for this chat.
5. Ask the agent to run a small Python cell. The execution approval identifies JupyterHub, the kernel and the directory. **Use local** closes the connection and switches the chat back to local execution.

## Try a small analysis

> Run a Jupyter cell that sets `values = [2, 4, 6]`. In the next cell, write their mean to `bat-example-mean.txt` in the kernel working directory and print the mean. Pause if the file already exists.

Expect `4.0` in the conversation. Refresh the chosen folder in JupyterLab to find the file.

| Action | Where the result goes |
| --- | --- |
| Create or edit a project file | The selected local BAT workspace. |
| Execute a Python cell | The connected remote kernel. Output is returned to the chat. |
| Execute a cell from a local `.ipynb` | BAT sends that code cell to the kernel and saves its supported outputs back to the same local notebook version. |
| Run a local `.py` file | BAT sends the script source and arguments. A child of the selected kernel's Python runs it in the remote working directory. It has its own variables. |
| Write a file from running Python | The remote filesystem. Use JupyterLab to upload inputs or download results. |

Read a local script or notebook before asking the agent to run it. BAT checks the source hash so an approval applies to the version read. If a notebook changes during execution, review the remote result before deciding whether to run the cell again. Scripts that import sibling files need those modules in the remote environment or working directory.

## Connection and output

- The loopback BAT service connects to the Hub over HTTPS and to its kernel channels over secure WebSocket. Both need a trusted server certificate and a reachable route. The first version supports default, same-origin user servers.
- The BAT interpreter needs the optional `tornado>=6.4,<7` dependency for remote execution. The selected remote kernel supplies the analysis libraries. Check the BAT environment with `scripts/check_environment.py`.
- Hub credentials stay in the running service's memory. The chat remembers its remote target across restarts and asks for reconnection. Switching workspace closes existing connections. Switching targets resets the conversation's execution grant.
- Code, selected source, and arguments go to the kernel. Tool results go into conversation history and may be sent to the configured model on the next step. Use an approved model and appropriate inputs.
- An interrupted or disconnected run may already have written files. Check JupyterLab before retrying. An abrupt BAT or network shutdown can leave a kernel in JupyterLab's Running panel.
- Text output is bounded. Notebook cells support streams, errors, plain text and size-limited PNG output. For large plots, rich widgets or long reports, save an artifact remotely and open it in JupyterLab.

The [JupyterHub REST guide](https://jupyterhub.readthedocs.io/en/stable/howto/rest.html) describes personal tokens. The [Jupyter Server API](https://jupyter-server.readthedocs.io/en/stable/developers/rest-api.html) documents the user-server endpoints.
