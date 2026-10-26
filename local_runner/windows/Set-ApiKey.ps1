param([string]$Workspace = (Join-Path $env:LOCALAPPDATA 'DailyArxiv'))
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Daily arXiv - DeepSeek API Key'
$form.Size = New-Object System.Drawing.Size(560, 255)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$label = New-Object System.Windows.Forms.Label
$label.Text = 'Enter your DeepSeek API key. It is encrypted on this PC using your Windows account. Do not send it in chat.'
$label.Location = New-Object System.Drawing.Point(20, 20)
$label.Size = New-Object System.Drawing.Size(505, 50)
$form.Controls.Add($label)
$box = New-Object System.Windows.Forms.TextBox
$box.Location = New-Object System.Drawing.Point(20, 85)
$box.Size = New-Object System.Drawing.Size(505, 25)
$box.UseSystemPasswordChar = $true
$form.Controls.Add($box)
$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save encrypted key'
$save.Location = New-Object System.Drawing.Point(335, 135)
$save.Size = New-Object System.Drawing.Size(190, 35)
$form.Controls.Add($save)
$form.AcceptButton = $save
$save.Add_Click({
    if ([string]::IsNullOrWhiteSpace($box.Text)) { return }
    try {
        New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
        $secretPath = Join-Path $Workspace 'api-key.dpapi'
        $secure = ConvertTo-SecureString $box.Text.Trim() -AsPlainText -Force
        $encrypted = ConvertFrom-SecureString $secure
        [IO.File]::WriteAllText($secretPath, $encrypted)
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.SetOwner($sid)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')))
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule('SYSTEM', 'FullControl', 'Allow')))
        Set-Acl -LiteralPath $secretPath -AclObject $acl
        $box.Clear()
        $secure.Dispose()
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [System.Windows.Forms.MessageBox]::Show('The encrypted key could not be saved. Please check access to the DailyArxiv folder.', 'Save failed') | Out-Null
    }
})
if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output 'Encrypted API key saved locally.'
} else {
    Write-Output 'No key was changed.'
}
$form.Dispose()
