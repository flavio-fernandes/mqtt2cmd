# coding: utf-8
# -*- mode: ruby -*-
# vi: set ft=ruby :

$bootstrap_debian = <<SCRIPT
# apt-get update && sudo apt-get -y upgrade && sudo apt-get -y dist-upgrade
apt update
apt upgrade -y
apt install -y python3-pip python3-venv
SCRIPT

$install_mosquitto = <<SCRIPT
apt install -y mosquitto mosquitto-clients

cat <<EOT > /etc/mosquitto/conf.d/localbroker.conf
allow_anonymous true
listener 1883 192.168.123.123
EOT

systemctl enable mosquitto
systemctl restart mosquitto
sleep 3
systemctl status --full --no-pager mosquitto
SCRIPT

$install_mqtt2cmd = <<SCRIPT
rm -rf /vagrant/env
/vagrant/mqtt2cmd/bin/create-env.sh
source /vagrant/env/bin/activate
pip install --upgrade pip
echo '[ -e /vagrant/env/bin/activate ] && source /vagrant/env/bin/activate' >> ~/.bashrc

ln -s /vagrant/data/config.yaml.vagrant ~/mqtt2cmd.config.yaml
sudo cp -v /vagrant/mqtt2cmd/bin/mqtt2cmd.service.vagrant /lib/systemd/system/mqtt2cmd.service
sudo systemctl enable --now mqtt2cmd.service

ln -s /vagrant/mqtt2cmd/bin/tail_log.sh ~/
ln -s /vagrant/mqtt2cmd/bin/reload_config.sh ~/
ln -s /vagrant/mqtt2cmd/tests/basic_test.sh.vagrant ~/basic_test.sh
SCRIPT

$test_mqtt2cmd = <<SCRIPT
sudo systemctl status --full --no-pager mqtt2cmd
~/basic_test.sh
SCRIPT


# All Vagrant configuration is done below. The "2" in Vagrant.configure
# configures the configuration version (we support older styles for
# backwards compatibility). Please don't change it unless you know what
# you're doing.
Vagrant.configure("2") do |config|

    vm_memory = ENV['VM_MEMORY'] || '512'
    vm_cpus = ENV['VM_CPUS'] || '2'

    config.vm.hostname = "mqtt2cmdVM"
    # config.vm.box = "generic/ubuntu2204"
    config.vm.box = "debian/bullseye64"
    # config.vm.box_check_update = false

    # config.vm.synced_folder "#{ENV['PWD']}", "/vagrant", disabled: false, type: "sshfs"
    # Optional: Uncomment line above and comment out the line below if you have
    # the vagrant sshfs plugin and would like to mount the directory using sshfs.
    config.vm.synced_folder ".", "/vagrant", type: "rsync"

    config.vm.network 'private_network', ip: "192.168.123.123"

    config.vm.provision "bootstrap_debian", type: "shell", inline: $bootstrap_debian
    config.vm.provision "install_mosquitto", type: "shell", inline: $install_mosquitto
    config.vm.provision "install_mqtt2cmd", type: "shell", inline: $install_mqtt2cmd, privileged: false
    config.vm.provision "test_mqtt2cmd", type: "shell", inline: $test_mqtt2cmd, privileged: false

    config.vm.provider 'libvirt' do |lb|
        lb.nested = true
        lb.memory = vm_memory
        lb.cpus = vm_cpus
        lb.suspend_mode = 'managedsave'
        #lb.storage_pool_name = 'images'
    end
    config.vm.provider "virtualbox" do |vb|
       vb.memory = vm_memory
       vb.cpus = vm_cpus
       vb.customize ["modifyvm", :id, "--nested-hw-virt", "on"]
       vb.customize ["modifyvm", :id, "--nictype1", "virtio"]
       vb.customize [
           "guestproperty", "set", :id,
           "/VirtualBox/GuestAdd/VBoxService/--timesync-set-threshold", 10000
          ]
    end
end
