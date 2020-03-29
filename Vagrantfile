# coding: utf-8
# -*- mode: ruby -*-
# vi: set ft=ruby :

# Vagrantfile API/syntax version.
VAGRANTFILE_API_VERSION = "2"
Vagrant.require_version ">=1.7.0"

$bootstrap_centos = <<SCRIPT
#yum -y update ||:  ; # save your time. "vagrant box update" is your friend
yum -y install epel-release python3 python3-libs python3-pip

SCRIPT

$install_mosquitto = <<SCRIPT
yum -y install epel-release mosquitto

# allow anonymous mqtt, but only on private_network
sed -i -E 's/#.*allow_anonymous\ [a-zA-Z]+$/allow_anonymous\ true/' /etc/mosquitto/mosquitto.conf
sed -i -E 's/#.*bind_address\ *$/bind_address 192.168.123.123/' /etc/mosquitto/mosquitto.conf

systemctl enable --now mosquitto
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


Vagrant.configure(2) do |config|

    vm_memory = ENV['VM_MEMORY'] || '512'
    vm_cpus = ENV['VM_CPUS'] || '2'

    config.vm.hostname = "mqtt2cmdVM"
    config.vm.box = "centos/7"
    config.vm.box_check_update = false

    # config.vm.synced_folder "#{ENV['PWD']}", "/vagrant", sshfs_opts_append: "-o nonempty", disabled: false, type: "sshfs"
    # Optional: Uncomment line above and comment out the line below if you have
    # the vagrant sshfs plugin and would like to mount the directory using sshfs.
    config.vm.synced_folder ".", "/vagrant", type: "rsync"

    config.vm.network 'private_network', ip: "192.168.123.123"

    config.vm.provision "bootstrap_centos", type: "shell", inline: $bootstrap_centos
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
